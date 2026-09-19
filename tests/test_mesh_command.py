# SPDX-License-Identifier: AGPL-3.0-only
"""``/mesh`` -- the message graph, started from a session.

tests/test_meshgraph.py owns the SERVER's own properties: the bind
address, the token gate, the untrusted body, the stream's cursor. None of
them is re-tested here. What this file pins is the command that starts
it, and every test is named for a failure the command could introduce
without the server noticing:

* **the posture survives the front door.** A command that reached
  ``MeshServer(host=...)`` with anything but loopback, or that printed a
  URL without the token, would defeat a boundary the server cannot
  defend on its own. So the URL the command hands the operator is
  FETCHED here, and the same address without the token is fetched too.
* **a chip that lies is worse than no chip.** A loopback HTTP server
  serving full message bodies is a second surface on this machine that
  the user started and can forget -- the same argument the remote-driver
  chip is written for. It has to appear when one is up and vanish when it
  is not.
* **the browser is not opened by default.** DOXA runs in terminals with
  no browser behind them, and a view that tried to open one there would
  at best do nothing and at worst paint a launcher's error over the TUI.
* **a run's graph is the run.** ``/mesh <run-id>`` must read that run's
  OWN ledger -- a fleet gets its own DOXA_HOME precisely so its graph
  contains the run and nothing of the operator's own sessions.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from doxa import config as config_mod
from doxa.app import DoxaApp, SystemBlock
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


@pytest.fixture(autouse=True)
def _no_server_left_behind():
    """A leaked MeshServer holds a loopback port and a daemon thread for
    the rest of the session. Every test here stops its own; this is the
    net under them."""
    servers: "list[object]" = []
    from doxa import meshgraph as meshgraph_mod

    real = meshgraph_mod.MeshServer

    class Tracked(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            servers.append(self)

    meshgraph_mod.MeshServer = Tracked  # type: ignore[misc]
    try:
        yield servers
    finally:
        meshgraph_mod.MeshServer = real  # type: ignore[misc]
        for server in servers:
            try:
                server.stop()
            except Exception:
                pass


def _system_texts(app) -> "list[str]":
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


async def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path)), fake


async def _run(app, pilot, line: str, pane=None) -> str:
    pane = pane or app.active_pane
    before = len(_system_texts(app))
    await pane._run_command(line)
    for _ in range(200):
        texts = _system_texts(app)
        if len(texts) > before:
            return texts[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no output block")


def _url_in(text: str) -> str:
    for word in text.split():
        if word.startswith("http://"):
            return word
    raise AssertionError(f"no URL in {text!r}")


def _chips(pane) -> "list[str]":
    return [chip.key for chip in pane._status_chips()]


def _mesh_chips(pane) -> "list[str]":
    """Matched on the GLYPH, not on the word: the repo-name chip carries
    the working directory, and a tmp_path with "mesh" in it would
    otherwise make this assertion pass or fail for the wrong reason (it
    did, on the first run of this file)."""
    from doxa.session.chips import MESH_GLYPH

    return [chip for chip in _chips(pane) if chip.startswith(MESH_GLYPH)]


# =======================================================================


@pytest.mark.asyncio
async def test_mesh_starts_a_token_gated_loopback_server_and_stop_ends_it(
    monkeypatch, tmp_path
):
    """The whole command, end to end and over a real socket: the URL it
    prints answers, the same address without the token does not, and
    ``/mesh stop`` closes the port."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/mesh")
        url = _url_in(text)
        assert url.startswith("http://127.0.0.1:"), url
        # The token is a PATH segment, which is what keeps mesh.js,
        # mesh.css, /ledger and /events inside it as relative URLs.
        server = app.mesh_server()
        assert server is not None
        assert url == f"http://127.0.0.1:{server.port}/{server.token}/"
        assert len(server.token) >= 20

        with urllib.request.urlopen(url, timeout=5) as res:
            assert res.status == 200

        base = f"http://127.0.0.1:{server.port}"
        for route in ("/", "/ledger", "/wrong-token/ledger"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + route, timeout=5)
            assert caught.value.code == 404, route

        note = await _run(app, pilot, "/mesh stop")
        assert "stopped" in note
        assert app.mesh_server() is None
        with pytest.raises(Exception):
            urllib.request.urlopen(url, timeout=2)

        assert "nothing is running" in await _run(app, pilot, "/mesh stop")


@pytest.mark.asyncio
async def test_the_mesh_chip_appears_while_the_server_is_up_and_not_before(
    monkeypatch, tmp_path
):
    """Hidden at zero, like the peers chip and the remote-driver chip
    beside it: the chip's ABSENCE is the statement that no local server is
    listening, and it must never paint blank."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert _mesh_chips(pane) == []

        await _run(app, pilot, "/mesh")
        mesh = _mesh_chips(pane)
        assert len(mesh) == 1
        assert mesh[0].strip()  # never blank
        assert str(app.mesh_server().port) in mesh[0]

        await _run(app, pilot, "/mesh stop")
        assert _mesh_chips(pane) == []


@pytest.mark.asyncio
async def test_the_browser_is_not_opened_unless_the_setting_says_so(
    monkeypatch, tmp_path
):
    """Off by default, because a headless or remote session has no
    browser to open and the URL is printed either way."""
    opened: "list[str]" = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not config_mod.mesh_open_browser()
        text = await _run(app, pilot, "/mesh")
        assert opened == []
        assert _url_in(text)  # printed regardless -- nothing is lost

        await _run(app, pilot, "/mesh stop")
        monkeypatch.setenv("DOXA_MESH_OPEN_BROWSER", "1")
        config_mod.invalidate()
        assert config_mod.mesh_open_browser()
        text = await _run(app, pilot, "/mesh")
        assert opened == [_url_in(text)]
        await _run(app, pilot, "/mesh stop")


@pytest.mark.asyncio
async def test_mesh_over_a_run_reads_that_runs_own_ledger(monkeypatch, tmp_path):
    """A fleet run gets its own DOXA_HOME so its graph IS the run. A
    /mesh that served the machine's ledger for a run id would show the
    operator's own sessions as participants in an experiment they were
    never in."""
    from doxa import fleet as fleet_mod
    from doxa import fleetview as fleetview_mod

    root = tmp_path / "fleetroot"
    run_root = root / "20260918T104355-3f2a"
    ledger = fleetview_mod.run_ledger_path(run_root)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        "v": 1, "id": "m0", "ts": "2026-09-18T10:00:00.000000Z",
        "from": {"session": "fleet-slot-0", "title": "t", "repo": "/r",
                 "model": "sonnet", "engine": "claude"},
        "to": ["fleet-slot-1"], "kind": "direct", "in_reply_to": None,
        "body": "ready", "body_sha256": "x", "latency_ms": None,
        "turn": {"id": None, "state": "idle"},
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(fleet_mod, "default_root", lambda: root)

    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # An unambiguous PREFIX, because a run id is a stamp nobody
        # retypes -- and a prefix matching nothing is refused in words.
        assert "no run matching" in await _run(app, pilot, "/mesh nosuchrun")

        text = await _run(app, pilot, "/mesh 20260918")
        assert "20260918T104355-3f2a" in text
        server = app.mesh_server()
        assert server.path == ledger

        with urllib.request.urlopen(_url_in(text) + "ledger", timeout=5) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert [r["body"] for r in payload["records"]] == ["ready"]
        assert payload["records"][0]["from"] == "fleet-slot-0"

        await _run(app, pilot, "/mesh stop")


@pytest.mark.asyncio
async def test_a_second_mesh_is_refused_rather_than_swapped(monkeypatch, tmp_path):
    """The running server holds a port and a token the operator may
    already have open in a browser. Silently re-pointing that URL at a
    different file is worse than saying no."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = _url_in(await _run(app, pilot, "/mesh"))
        again = await _run(app, pilot, "/mesh")
        assert "already up" in again
        assert first in again
        assert _url_in(again) == first
        await _run(app, pilot, "/mesh stop")


@pytest.mark.asyncio
async def test_closing_the_window_releases_the_port(monkeypatch, tmp_path):
    """A TUI that exits leaving a loopback server serving message bodies
    is the "silent second surface" this whole posture exists to avoid."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        url = _url_in(await _run(app, pilot, "/mesh"))
        assert app.mesh_server() is not None
    assert app.mesh_server() is None
    with pytest.raises(Exception):
        urllib.request.urlopen(url, timeout=2)
