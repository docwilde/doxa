# SPDX-License-Identifier: AGPL-3.0-only
"""Image-ladder tests (doxa/images.py + its two render sites).

The ladder is unit-tested by forcing each tier -- via the DOXA_IMAGE_MODE
env override and via the probe seams -- and the render sites (tool_result
image_path, /img) are pilot-tested for widget-vs-fallback selection. NO
pixel output is asserted anywhere: rendering fidelity is textual-image's
business; doxa's contract is that every site always mounts SOMETHING and
that the something degrades to the text line.
"""

from __future__ import annotations

import base64

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from doxa import images
from doxa.app import DoxaApp, ImageBlock, SystemBlock, ToolChip
from doxa.engine import EngineEvent, SessionEngine
from tests.fakes import FakeEngine, factory_with_script

# 1x1 red PNG, pre-encoded -- no PIL needed to build fixtures.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAF"
    "AAH/q842iQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(TINY_PNG)
    return p


# -- ladder selection ---------------------------------------------------


def test_env_override_forces_each_tier(monkeypatch, png):
    from textual.widgets import Static
    from textual_image.widget import HalfcellImage, SixelImage, TGPImage

    expectations = {
        "kgp": TGPImage, "sixel": SixelImage, "halfblock": HalfcellImage,
    }
    for mode, cls in expectations.items():
        monkeypatch.setenv("DOXA_IMAGE_MODE", mode)
        assert images.detect_mode() == mode
        widget = images.widget_for(str(png), "desc")
        assert isinstance(widget, cls)

    monkeypatch.setenv("DOXA_IMAGE_MODE", "text")
    widget = images.widget_for(str(png), "the picture")
    assert isinstance(widget, Static)
    assert str(widget.renderable) == "[image: the picture]"


def test_probe_ladder_order(monkeypatch):
    """KGP wins over sixel wins over half-block; no tty means text; a
    probe explosion means text -- never an exception."""
    monkeypatch.setattr(images, "_is_tty", lambda: True)
    monkeypatch.setattr(images, "_kgp_support", lambda: True)
    monkeypatch.setattr(images, "_sixel_support", lambda: True)
    assert images._probe() == "kgp"

    monkeypatch.setattr(images, "_kgp_support", lambda: False)
    assert images._probe() == "sixel"

    monkeypatch.setattr(images, "_sixel_support", lambda: False)
    assert images._probe() == "halfblock"

    monkeypatch.setattr(images, "_is_tty", lambda: False)
    assert images._probe() == "text"

    def boom() -> bool:
        raise RuntimeError("terminal went away")

    monkeypatch.setattr(images, "_is_tty", lambda: True)
    monkeypatch.setattr(images, "_kgp_support", boom)
    assert images._probe() == "text"


def test_probe_runs_at_most_once(monkeypatch):
    calls = []

    def probe_once():
        calls.append(1)
        return "halfblock"

    monkeypatch.delenv("DOXA_IMAGE_MODE", raising=False)
    monkeypatch.setattr(images, "_probe", probe_once)
    monkeypatch.setattr(images, "_detected", None)
    assert images.detect_mode() == "halfblock"
    assert images.detect_mode() == "halfblock"
    assert calls == [1]


def test_widget_for_bad_source_degrades_to_fallback(monkeypatch, tmp_path):
    """A tier that should render pixels but cannot open the file must yield
    the text fallback, not raise."""
    from textual.widgets import Static

    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    widget = images.widget_for(str(tmp_path / "missing.png"), "missing.png")
    assert isinstance(widget, Static)
    assert "[image: missing.png]" in str(widget.renderable)


def test_looks_like_image_path(png, tmp_path):
    assert images.looks_like_image_path(str(png)) is True
    assert images.looks_like_image_path(f"  {png}  ") is True
    assert images.looks_like_image_path(str(tmp_path / "nope.png")) is False
    assert images.looks_like_image_path("not a path at all") is False
    assert images.looks_like_image_path(f"two lines\n{png}") is False
    assert images.looks_like_image_path("") is False


# -- engine convention: tool_result gains image_path --------------------


@pytest.mark.asyncio
async def test_tool_result_path_payload_sets_image_path(tmp_path, png):
    script = [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Screenshot", input={})],
            model="claude-haiku-4-5",
        ),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="t1", content=str(png), is_error=False,
        )]),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("grab it")]
    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.data["image_path"] == str(png)
    await engine.finalize()


@pytest.mark.asyncio
async def test_tool_result_inline_image_bytes_materialize(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    inline = [{
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png",
                   "data": base64.b64encode(TINY_PNG).decode("ascii")},
    }]
    script = [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Screenshot", input={})],
            model="claude-haiku-4-5",
        ),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="t1", content=inline, is_error=False,
        )]),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("grab it")]
    tool_result = next(e for e in events if e.type == "tool_result")
    from pathlib import Path

    materialized = Path(tool_result.data["image_path"])
    assert materialized.is_file()
    assert materialized.read_bytes() == TINY_PNG
    await engine.finalize()


@pytest.mark.asyncio
async def test_plain_text_tool_result_has_no_image_path(tmp_path):
    script = [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={})],
            model="claude-haiku-4-5",
        ),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="t1", content="just text", is_error=False,
        )]),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("run")]
    tool_result = next(e for e in events if e.type == "tool_result")
    assert "image_path" not in tool_result.data
    await engine.finalize()


# -- render sites --------------------------------------------------------


def _image_turn_script(image_path: str) -> list[EngineEvent]:
    return [
        EngineEvent("turn_started", {}),
        EngineEvent("tool_call", {"id": "t1", "name": "Screenshot", "input": {}}),
        EngineEvent("tool_result", {
            "id": "t1", "name": "Screenshot", "result_summary": image_path,
            "is_error": False, "duration_ms": 5, "image_path": image_path,
        }),
        EngineEvent("turn_done", {
            "cost_usd": 0.0, "duration_ms": 10, "is_error": False,
            "session_cost_usd": 0.0, "ctx_percentage": 1.0,
        }),
    ]


async def _run_one_turn(app, pilot):
    app.query_one("#prompt-input").value = "screenshot please"
    await pilot.press("enter")
    for _ in range(100):
        chips = list(app.query(ToolChip))
        if chips and chips[0].tool_result is not None:
            return chips[0]
        await pilot.pause(0.02)
    raise AssertionError("tool chip never completed")


@pytest.mark.asyncio
async def test_tool_chip_mounts_fallback_text_on_text_tier(monkeypatch, tmp_path, png):
    monkeypatch.setenv("DOXA_IMAGE_MODE", "text")
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(_image_turn_script(str(png))),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        chip = await _run_one_turn(app, pilot)
        assert chip.tool_image_path == str(png)
        # Lazy: nothing mounted before the first expand.
        assert chip._image_mounted is False
        chip.collapsed = False
        await pilot.pause()
        assert chip._image_mounted is True
        fallbacks = list(chip.query(".image-fallback"))
        assert len(fallbacks) == 1
        assert f"[image: {png}]" in str(fallbacks[0].renderable)


@pytest.mark.asyncio
async def test_tool_chip_mounts_image_widget_on_halfblock_tier(monkeypatch, tmp_path, png):
    from textual_image.widget import HalfcellImage

    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine(_image_turn_script(str(png))),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        chip = await _run_one_turn(app, pilot)
        chip.collapsed = False
        await pilot.pause()
        assert len(list(chip.query(HalfcellImage))) == 1
        assert not list(chip.query(".image-fallback"))


@pytest.mark.asyncio
async def test_img_command_mounts_block_with_fallback(monkeypatch, tmp_path, png):
    monkeypatch.setenv("DOXA_IMAGE_MODE", "text")
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([])
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = f"/img {png}"
        await pilot.press("enter")
        for _ in range(100):
            if list(app.query(ImageBlock)):
                break
            await pilot.pause(0.02)
        blocks = list(app.query(ImageBlock))
        assert len(blocks) == 1
        assert str(png) in str(blocks[0].query_one(".image-caption").renderable)
        assert f"[image: {png}]" in str(
            blocks[0].query_one(".image-fallback").renderable
        )
        # A slash command is never a turn.
        assert not app.query("TurnBlock")


@pytest.mark.asyncio
async def test_img_command_missing_file_and_bare_showcase(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([])
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()

        def _blocks():
            return [b for b in app.query(SystemBlock) if b.id != "identity-block"]

        app.query_one("#prompt-input").value = f"/img {tmp_path}/nope.png"
        await pilot.press("enter")
        for _ in range(100):
            if _blocks():
                break
            await pilot.pause(0.02)
        assert "no such file" in _blocks()[0].text

        # Bare /img is the showcase since v0.41.0, not a usage error -- but
        # it must still never mount an ImageBlock, which is the /img <path>
        # widget and belongs to a path the user did not give.
        from doxa.app import ImageShowcaseBlock

        app.query_one("#prompt-input").value = "/img"
        await pilot.press("enter")
        for _ in range(100):
            if app.query(ImageShowcaseBlock):
                break
            await pilot.pause(0.02)
        assert app.query(ImageShowcaseBlock)
        assert not app.query(ImageBlock)
