# SPDX-License-Identifier: AGPL-3.0-only
"""scripts/screenshot.py's own driver helpers, against a real Pilot.

The gallery script is the only thing that regenerates assets/shots, so
until v1.7.1 the only thing that tested it was running it -- and its one
genuine defect was invisible that way, because it did not fail the script,
it made the script SLOW and flaky: `_activate` switched the visible tab
and said nothing about where the keyboard went, which is the one thing
v0.38.0 forbids and every caller in doxa/app.py obeys.

What that cost, measured at v1.7.1 (textual 5.3.0): hiding the TabPane the
keyboard is in makes Textual re-home focus (`Screen._reset_focus`) onto
another focusable widget INSIDE that same just-hidden pane; focusing a
widget inside a TabPane re-ACTIVATES it
(`TabbedContent._on_tab_pane_focused`), hiding the other tab;
`DoxaApp._on_tab_activated` then moves the keyboard into the newly active
tab's prompt -- back inside whichever pane is about to be hidden next.
The window never goes idle again. Since `_fill_hero_conversation` calls
`_activate` and nearly every scene calls `_fill_hero_conversation`, every
later `pilot.pause`-polled wait in the gallery was racing a busy pump:
`split-panes` failed about half its runs, the whole gallery could not
complete, and `live-diff` intermittently saw "no turn in flight".

So the helper gets a test of its own, at the level the defect lived at.

And the SCENES registry gets the same integrity pass
tests/test_record_gif.py gives its sibling's: names, sizes and the pairing
between a scene and the files it is the only thing that can regenerate.
Rendering a scene stays what `uv run python scripts/screenshot.py` does --
these tests cost no pilot and no inkscape, and catch the class of rot that
left `beliefs-browser.png` in the gallery for eighteen releases after the
surface it showed had been removed and its scene deleted with it.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from doxa import config as config_mod
from doxa.app import DoxaApp, SessionPane
from doxa.ui.prompt import PromptInput
from scripts import mesh_shot, screenshot
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _app(tmp_path):
    def make() -> FakeEngine:
        return FakeEngine([])

    return DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
    )


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _prompt_of(pane: SessionPane) -> PromptInput:
    return pane.query_one("#prompt-input", PromptInput)


async def _three_tabs(app: DoxaApp, pilot):
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    assert await _wait(pilot, lambda: len(app.panes()) == 3)
    await pilot.pause()
    return app.panes()


@pytest.mark.asyncio
async def test_activate_takes_the_keyboard_with_it(tmp_path):
    """The regression. Fails deterministically before the fix.

    The keyboard must arrive AND STAY. Arrival alone is not the assertion
    to make here: while the activation/focus loop was running, focus
    visited this very prompt every other turn on its way past, so a
    one-shot `app.focused is ...` poll passed on the broken code. Twenty
    consecutive quiet turns is the difference between "the keyboard is
    here" and "the keyboard is going round in circles through here"."""
    app = _app(tmp_path)
    async with app.run_test(size=(160, 48)) as pilot:
        panes = await _three_tabs(app, pilot)
        first = panes[0]
        assert app.focused is not _prompt_of(first)

        screenshot._activate(app, first)

        assert await _wait(pilot, lambda: app.focused is _prompt_of(first))
        assert app.active_pane is first
        for _ in range(20):
            await pilot.pause(0.02)
            assert app.focused is _prompt_of(first), (
                "the keyboard left the tab _activate switched to"
            )


@pytest.mark.asyncio
async def test_activate_leaves_the_window_idle(tmp_path):
    """The same fix stated as the symptom it actually caused: after
    `_activate`, nothing keeps moving the keyboard on its own.

    Counted rather than timed, so it is deterministic: focus moves during
    message-pump turns in which the test asks the app for nothing. Before
    the fix this was ~90 and unbounded; a settled window makes none."""
    app = _app(tmp_path)
    async with app.run_test(size=(160, 48)) as pilot:
        panes = await _three_tabs(app, pilot)

        screenshot._activate(app, panes[0])

        # Let the deliberate move land, then watch a quiet window.
        for _ in range(10):
            await pilot.pause(0.02)
        seen = [0]
        screen = app.screen
        original = screen.set_focus

        def counting(widget, scroll_visible=True, from_app_focus=False):
            seen[0] += 1
            return original(
                widget,
                scroll_visible=scroll_visible,
                from_app_focus=from_app_focus,
            )

        screen.set_focus = counting  # type: ignore[method-assign]
        for _ in range(40):
            await pilot.pause(0.02)

        assert seen[0] == 0, (
            f"{seen[0]} unasked-for focus moves in 40 idle turns -- the "
            f"activation/focus loop is back"
        )


# =======================================================================
# The registry, and the files it is the only thing that can regenerate
# =======================================================================

SHOTS = Path(screenshot.ROOT) / "assets" / "shots"


def _png_size(path: Path) -> "tuple[int, int]":
    """Width and height out of the IHDR, without decoding the image."""
    header = path.read_bytes()[16:24]
    return struct.unpack(">II", header)


def test_scene_names_are_unique_and_nonempty():
    names = [scene.name for scene in screenshot.SCENES]
    assert names, "no scenes registered"
    assert all(names), "a scene has an empty name"
    assert len(names) == len(set(names)), "duplicate scene name"


def test_every_scene_has_a_size_and_a_driver():
    for scene in screenshot.SCENES:
        cols, rows = scene.size
        assert cols > 0 and rows > 0, scene.name
        assert callable(scene.drive), f"scene {scene.name!r} has no driver"


def test_every_scene_shares_the_gallery_geometry():
    """One size for every still, which is v0.67.0's rule and the reason
    `WIDE` exists: the gallery visibly changed pixel size scene to scene
    before it, and ratio uniformity is not size uniformity."""
    for scene in screenshot.SCENES:
        assert scene.size == screenshot.WIDE, (
            f"scene {scene.name!r} is {scene.size}, not the shared "
            f"{screenshot.WIDE}"
        )


def test_scene_names_match_their_asset_filenames():
    """Names double as `assets/shots/<name>.svg`, the same convention
    tests/test_record_gif.py holds the animated scenes to."""
    for scene in screenshot.SCENES:
        assert scene.name == scene.name.lower(), scene.name
        assert " " not in scene.name, scene.name
        assert "/" not in scene.name, scene.name


def test_every_scene_has_a_committed_svg_and_png():
    for scene in screenshot.SCENES:
        svg = SHOTS / f"{scene.name}.svg"
        png = SHOTS / f"{scene.name}.png"
        assert svg.exists(), f"scene {scene.name!r} has no committed SVG"
        assert png.exists(), f"scene {scene.name!r} has no committed PNG"
        assert _png_size(png) == _png_size(SHOTS / "hero.png"), (
            f"{png.name} is not the size the rest of the gallery is"
        )


def test_every_committed_still_has_a_scene_that_can_regenerate_it():
    """The rot guard, named for the file that earned it: `beliefs-browser`
    showed a surface v0.69.0 removed, its generating scene went with the
    feature, and the two files sat in `assets/shots/` for eighteen
    releases with nothing able to refresh them. A file named for a feature
    is a claim the feature is still there."""
    names = {scene.name for scene in screenshot.SCENES}
    orphans = sorted(
        path.stem for path in SHOTS.glob("*.svg") if path.stem not in names
    )
    assert not orphans, (
        f"committed stills with no scene to regenerate them: {orphans}"
    )


def test_the_mesh_png_is_the_one_asset_with_no_svg_twin():
    """Stated as a test because it is the one exception to the rule above
    and an exception nobody records becomes a bug report. The mesh graph
    is a browser page drawn on a canvas: Chrome rasterises, and there is
    no vector form to export. scripts/mesh_shot.py is what regenerates
    it."""
    png = SHOTS / "mesh.png"
    assert png.exists(), "assets/shots/mesh.png is missing"
    assert not (SHOTS / "mesh.svg").exists(), (
        "mesh.svg exists -- if the page grew a vector export, this test "
        "and scripts/mesh_shot.py's docstring both need rewriting"
    )
    assert _png_size(png) == _png_size(SHOTS / "hero.png")
    assert _png_size(png) == (
        mesh_shot.WINDOW[0] * mesh_shot.SCALE,
        mesh_shot.WINDOW[1] * mesh_shot.SCALE,
    )


# =======================================================================
# The fleet scene's own two files
# =======================================================================


def test_the_fleet_scene_writes_a_run_that_reads_back_consistently():
    """The scene is a picture of numbers, so the numbers have to agree.

    It writes a manifest and a ledger and points the tab at them, which
    means nothing else checks them: a traffic table edited to say more
    than the manifest counts, or a dispatch spread wider than the
    quiescence it is supposed to sit inside, would ship as a plausible
    screenshot of an impossible run."""
    from doxa import fleetview as fleetview_mod

    run_root = screenshot._fleet_run_root()
    snapshot = fleetview_mod.RunSnapshot.read(run_root)
    manifest = snapshot.manifest
    text = fleetview_mod.render(snapshot)

    # The ledger the scene wrote IS the count the panel names.
    written = len(screenshot._FLEET_TRAFFIC)
    assert manifest["ledger"]["messages"] == written
    rows = fleetview_mod.run_ledger_path(run_root).read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(rows) == written
    assert f"of {written} message(s)" in text

    # One row per slot, every engine in the pool on screen, and the
    # memory-off count the spec asked for.
    n = manifest["spec"]["n"]
    assert len(snapshot.slots) == n
    assert sorted(slot["index"] for slot in snapshot.slots) == list(range(n))
    for engine in ("claude", "codex", "deepseek", "glm"):
        assert engine in screenshot._FLEET_POOL
        assert f"  {engine}" in text, f"{engine} was dealt no slot"
    off = [s for s in snapshot.slots if not s["assignment"]["lore"]]
    assert len(off) == manifest["spec"]["memory_off"] == 3
    assert text.count("OFF") == len(off)

    # The shape of the run, and the shape of the table that describes it.
    # Both arrived with supervisor mode (v1.15.0) and neither is optional
    # for this fixture: a scene that wrote a manifest the renderer has
    # moved on from would still render, into a picture of a table the
    # code no longer produces.
    assert "mode symmetric" in text, "the run's shape is not on the panel"
    assert "role" in fleetview_mod.assignment_table(snapshot)[0]
    assert text.count("worker") >= n, "a slot rendered without a role"
    assert "supervisor" not in text, (
        "this fixture is a symmetric run; nothing in it holds a prompt "
        "for anybody else"
    )

    # A start measured in milliseconds, inside a run measured in seconds.
    assert manifest["dispatch_spread_s"] < manifest["quiescence_s"]
    assert len(manifest["dispatch_order"]) == n
    assert sorted(manifest["dispatch_order"]) == list(range(n))

    # What a clean run must be able to say about itself.
    assert manifest["leaked_pids"] == []
    assert "leaked pids: none" in text
    assert "quiesced after" in text

    # Every sender in the tail is a session in the table -- a body
    # attributed to nobody would be the one thing in this panel a reader
    # could not check.
    ids = {slot["session_id"] for slot in snapshot.slots}
    for row in rows:
        record = json.loads(row)
        assert record["from"]["session"] in ids
        assert set(record["to"]) <= ids - {record["from"]["session"]}


def test_the_committed_fleet_still_shows_the_table_the_renderer_makes():
    """The staleness guard, and the reason this file knows about an SVG at
    all.

    A committed still is a claim about what the app renders, and the one
    thing that can quietly falsify it is the renderer changing shape:
    supervisor mode added a role column to the assignment table and a mode
    line above it, and every gallery image taken before that shows a table
    the code no longer produces. Nothing else notices -- the scene still
    runs, the asset still exists, and only a reader comparing the picture
    to the app finds out.

    Textual's SVG export writes the rendered characters, so the check is
    the header itself, whitespace-normalised: regenerate with `uv run
    python scripts/screenshot.py fleet` when this fails, which is the
    fix rather than a reason to loosen the assertion."""
    import html

    from doxa import fleetview as fleetview_mod

    snapshot = fleetview_mod.RunSnapshot.read(screenshot._fleet_run_root())
    committed = " ".join(
        html.unescape((SHOTS / "fleet.svg").read_text(encoding="utf-8"))
        .replace("\u00a0", " ").split()
    )
    for line in (
        fleetview_mod.assignment_table(snapshot)[0],
        fleetview_mod.mode_line(snapshot).splitlines()[0],
    ):
        wanted = " ".join(line.split())
        assert wanted in committed, (
            f"assets/shots/fleet.svg does not show {wanted!r} -- the "
            f"renderer has changed shape since the still was taken; "
            f"re-run scripts/screenshot.py fleet"
        )


def test_no_fleet_message_is_truncated_in_the_panel():
    """`fleetview.BODY_WIDTH` truncates a body to keep one message to one
    line, which is right for a live run and wrong for a still: a gallery
    image ending a sentence mid-word reads as a rendering fault. So the
    scene's own bodies are written to fit."""
    for _offset, _sender, _targets, body in screenshot._FLEET_TRAFFIC:
        from doxa import fleetview as fleetview_mod

        assert len(body) <= fleetview_mod.BODY_WIDTH, body


# =======================================================================
# The mesh scene's ledger
# =======================================================================


def test_the_mesh_ledger_parses_as_the_page_reads_it(tmp_path):
    """Written by scripts/mesh_shot.py, read by doxa.meshgraph: a record
    the parser drops is an edge that never reaches the page, silently and
    without an error anywhere."""
    from doxa import meshgraph as meshgraph_mod

    path = tmp_path / "ledger.jsonl"
    written = mesh_shot.write_ledger(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == written

    records = [meshgraph_mod.parse_record(line) for line in lines]
    assert all(record is not None for record in records), (
        "a line the page would drop"
    )
    senders = {record["from"] for record in records}
    assert senders == {session[0] for session in mesh_shot._SESSIONS}


def test_the_mesh_graph_has_an_edge_that_crosses_vendors():
    """The claim this asset is evidence for. A picture of nine Claude
    sessions would be a true picture of the view and no evidence at all
    that a mixed fleet messaged across vendors, and an edit to the traffic
    table that quietly lost the last cross-vendor pair would leave the
    README's caption describing something the image no longer shows."""
    engines = {session[0]: session[3] for session in mesh_shot._SESSIONS}
    assert len(set(engines.values())) == 4

    crossings = set()
    for _ago, sender, targets, _body in mesh_shot._TRAFFIC:
        recipients = (
            [i for i in range(len(mesh_shot._SESSIONS)) if i != sender]
            if targets is None else targets
        )
        for target in recipients:
            pair = (
                mesh_shot._SESSIONS[sender][3],
                mesh_shot._SESSIONS[target][3],
            )
            if pair[0] != pair[1]:
                crossings.add(pair)
    assert len(crossings) >= 4, crossings
