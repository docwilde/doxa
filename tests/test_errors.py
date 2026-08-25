"""The error surface (v0.56.0) -- doxa/errors.py, ErrorBlock, and the
app-level boundary that catches what Textual would otherwise exit on.

Every assertion about the block is a USER-VISIBLE one, on the v0.28.0 rule
this project has now paid for twice: "the widget is in the DOM" and "the
user can see it" are different claims, and a block nobody has ever looked
at is precisely where a zero-height regression hides for a release. So the
tests here read ``region.height``, the rendered title text, and the height
of the fold AFTER it is opened -- never merely that a query matched.

The four defects of 2026-08-24 are the specification:

  * a ``TimeoutError`` out of ``textual_image`` during a widget RENDER
    killed the app -> ``test_the_reported_render_crash_no_longer_kills``
  * the needs-input dialog wedged the session in silence
    -> ``test_a_failed_answer_delivery_is_reported_and_says_what_to_do``
  * server-tool results vanished with no trace -> the general rule that
    every caught exception becomes a block (``test_nothing_is_swallowed``)
  * the memory chip drew half of itself -> the quarantine SAYS what it
    hid (``test_quarantine_says_what_it_hid``)
"""

from __future__ import annotations

import asyncio
import types

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from doxa import errors
from doxa.app import DoxaApp, ErrorBlock, SystemBlock

# tests/conftest.py's _errors_must_be_claimed guard: every OTHER module in
# the suite fails if it quietly produced an error block, because a surface
# that turns a crash into a survivable block is one step away from turning
# a crash into a quietly passing test. This module provokes failures on
# purpose and asserts on every one of them, so it opts out of the blanket
# guard and into its own assertions.
EXPECTS_FAILURES = True

# A credential-shaped string. scrub_secrets must never let this reach a
# widget, the log file or the terminal -- a crash report that leaks a token
# is a worse defect than the crash it describes.
SECRET = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLL"


async def _settle(pilot, tries: int = 40) -> None:
    for _ in range(tries):
        await pilot.pause(0.02)


def _blocks(app) -> "list[ErrorBlock]":
    return list(app.query(ErrorBlock))


async def _booted(app, pilot) -> VerticalScroll:
    await _settle(pilot)
    return app.active_pane.query_one("#block-list", VerticalScroll)


async def _paint(app) -> None:
    """Force one repaint through a GUARDED caller, and never through
    ``Pilot.pause``.

    This matters, and it is not a convenience. In a running DOXA the
    screen refresh is a Textual ``Timer`` callback
    (``Screen.__init__``'s ``set_interval(..., self._on_timer_update,
    name="screen_update")``), and ``Timer._tick`` wraps every callback in
    ``except Exception as error: app._handle_exception(error)``
    (textual/timer.py:194) -- which is the boundary this release
    overrides, so a render raise is contained. ``Pilot.pause`` calls
    ``app.screen._on_timer_update()`` DIRECTLY (textual/pilot.py:533),
    outside that guard, so under the test harness alone a render raise
    lands in the test instead of in the app.

    ``call_later`` puts the same refresh back inside a guarded caller --
    ``MessagePump._flush_next_callbacks``, which funnels into
    ``app._handle_exception`` at message_pump.py:682 exactly as the timer
    does. Same containment, deterministically reached."""
    app.call_later(app.screen._on_timer_update)
    for _ in range(20):
        await asyncio.sleep(0.02)


class Exploding(Static):
    """A widget that raises when Textual asks it to paint -- the shape of
    the reported crash, with the third-party library taken out of it."""

    def render(self):
        raise TimeoutError("capture_terminal_response: no reply from terminal")


# -- the record itself -------------------------------------------------


def test_a_failure_is_scrubbed_at_construction_not_at_display():
    """Display, the log and the clipboard are three doors out of the
    process; scrubbing at each door means a fourth added later leaks."""
    try:
        raise RuntimeError(f"auth failed with {SECRET}")
    except RuntimeError as exc:
        failure = errors.from_exception(exc)
    assert SECRET not in failure.summary
    assert SECRET not in failure.detail
    assert SECRET not in failure.log_text()
    assert "REDACTED" in failure.summary


def test_a_traceback_never_carries_frame_locals():
    """Textual's own fatal path prints Traceback(show_locals=True), and
    the locals are where a credential actually lives."""
    def inner():
        api_key = SECRET  # noqa: F841 -- the point of the test
        raise ValueError("boom")

    try:
        inner()
    except ValueError as exc:
        failure = errors.from_exception(exc)
    assert "boom" in failure.detail
    assert "api_key" not in failure.detail
    assert SECRET not in failure.detail


def test_detail_is_bounded():
    """A RecursionError's traceback is megabytes of identical frames, and
    neither a transcript block nor a rotating log is the place to discover
    that."""
    long_detail = "\n".join(
        f'  File "doxa/session/runtime.py", line {n}, in _handle_event'
        for n in range(errors.DETAIL_MAX_CHARS)
    )
    built = errors.policy_failure("doxa", "deep stack", long_detail)
    assert len(long_detail) > errors.DETAIL_MAX_CHARS * 3
    assert len(built.detail) < errors.DETAIL_MAX_CHARS + 200
    assert "not shown" in built.detail
    # The dataclass itself does not bound -- the builders do, so that a
    # caller cannot construct one that skipped the scrub either.
    raw = errors.Failure("doxa", "x", long_detail)
    assert len(raw.detail) > errors.DETAIL_MAX_CHARS


def test_a_long_exception_message_does_not_become_the_headline():
    try:
        raise RuntimeError("x" * 5000)
    except RuntimeError as exc:
        failure = errors.from_exception(exc)
    assert len(failure.summary) < 200
    assert failure.summary.startswith("RuntimeError: ")


def test_the_signature_is_stable_across_repeats_of_one_failure():
    """Two paints of the same broken widget produce byte-different
    tracebacks and must still count as one thing."""
    made = []
    for _ in range(2):
        try:
            raise TimeoutError("no reply")
        except TimeoutError as exc:
            made.append(errors.from_exception(exc, context="painting"))
    assert made[0].signature == made[1].signature
    assert made[0].detail != "" and made[1].detail != ""


# -- attribution -------------------------------------------------------


def test_origin_names_doxa_for_doxas_own_code():
    try:
        errors._bound(None)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        assert errors.origin_of(exc) == errors.DOXA
    else:
        pytest.fail("_bound tolerated a non-string")


def test_origin_names_lore_for_the_in_process_belief_store():
    """lore_core runs INSIDE doxa, and a LORE-side raise has had nowhere
    legible to land for as long as that has been true."""
    import lore_core.scrub as scrub_mod

    try:
        scrub_mod.scrub_secrets(object())  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        assert errors.origin_of(exc) == errors.LORE
    else:
        pytest.skip("scrub_secrets tolerated a non-string")


def test_origin_skips_textual_and_the_standard_library():
    """Textual is the harness every DOXA frame runs inside and the stdlib
    is what a defect passes THROUGH -- neither is ever the culprit while
    somebody else's frame is on the stack."""
    import json

    try:
        json.loads("{ not json")
    except Exception as exc:  # noqa: BLE001
        # The deepest frames are json's own; the deepest NON-infrastructure
        # frame is this test module.
        assert errors.origin_of(exc) == __name__.split(".", 1)[0]


def test_origin_falls_back_to_unknown_rather_than_to_nothing():
    assert errors.origin_of(RuntimeError("never raised")) == errors.UNKNOWN


def test_an_explicit_origin_always_wins():
    """The one thing a future plugin loader has to pass so a plugin's
    crash does not read as a DOXA bug."""
    try:
        raise RuntimeError("hook raised")
    except RuntimeError as exc:
        failure = errors.from_exception(exc, origin="plugin:jira")
    assert failure.origin == "plugin:jira"
    assert "plugin:jira" in failure.headline()


# -- failure is more general than exception ----------------------------


def test_a_policy_violation_is_a_failure_with_no_exception_behind_it():
    """docs/plugin-api.md's third failure state: a chip whose text()
    overruns its time budget did not raise, it broke a promise, and it is
    disabled just as loudly."""
    failure = errors.policy_failure(
        "plugin:jira",
        "status chip text() took 900ms — the budget is 50ms",
        "disabled for the rest of this run",
    )
    assert failure.kind == errors.KIND_POLICY
    assert failure.detail
    block = ErrorBlock(failure)
    assert "plugin:jira" in block.title
    assert "900ms" in block.title


# -- the queryable state a loader would read ---------------------------


def test_the_failure_log_is_state_and_not_just_messages():
    log = errors.FailureLog()
    jira = errors.policy_failure("plugin:jira", "hook raised")
    assert log.failed("plugin:jira") is False
    assert log.record(jira) == 1
    assert log.record(jira) == 2
    assert log.failed("plugin:jira") is True
    assert log.origins() == {"plugin:jira": 2}
    assert log.total() == 2
    # A copy: a caller must not be able to edit the tally.
    log.origins()["plugin:jira"] = 99
    assert log.origins() == {"plugin:jira": 2}


def test_repeats_do_not_retain_a_traceback_each():
    log = errors.FailureLog()
    for _ in range(500):
        log.record(errors.policy_failure("noisy", "same thing"))
    assert len(log.recent) == 1
    assert log.origins() == {"noisy": 500}


def test_distinct_failures_are_kept_but_bounded():
    log = errors.FailureLog()
    for index in range(errors.FailureLog.RECENT_MAX * 3):
        log.record(errors.policy_failure("noisy", f"thing {index}"))
    assert len(log.recent) == errors.FailureLog.RECENT_MAX


# -- the log on disk ---------------------------------------------------


def test_the_log_lives_under_doxas_own_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    assert errors.log_path().parent == tmp_path / "home"
    assert errors.log_path().name == "errors.log"


def test_a_failure_is_persisted_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    try:
        raise RuntimeError(f"token {SECRET}")
    except RuntimeError as exc:
        written = errors.append(errors.from_exception(exc))
    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "REDACTED" in text
    assert "origin=" in text and "fatal=no" in text


def test_the_log_is_bounded_by_size_with_one_generation(tmp_path, monkeypatch):
    """Bounded by SIZE and not by age, one previous generation, so the
    whole on-disk cost is twice LOG_MAX_BYTES and needs no sweeper."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    path = errors.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * (errors.LOG_MAX_BYTES + 10), encoding="utf-8")
    errors.append(errors.policy_failure("doxa", "after the rotation"))
    assert errors.rotated_path().exists()
    assert path.stat().st_size < errors.LOG_MAX_BYTES
    assert "after the rotation" in path.read_text(encoding="utf-8")
    # A second rotation replaces the one generation rather than growing a
    # third -- the ceiling is two files, always.
    path.write_text("y" * (errors.LOG_MAX_BYTES + 10), encoding="utf-8")
    errors.append(errors.policy_failure("doxa", "after the second"))
    assert not errors.log_path().with_name(errors.LOG_NAME + ".2").exists()


def test_an_unwritable_log_never_hides_the_failure(tmp_path, monkeypatch):
    """The block is mounted BEFORE this is called, so a full disk costs
    the persisted copy and never the visible one."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "nope" / "home"))
    monkeypatch.setattr(
        errors.Path, "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert errors.append(errors.policy_failure("doxa", "x")) is None


# -- the block a user actually sees ------------------------------------


@pytest.mark.asyncio
async def test_a_failed_worker_produces_a_visible_block(tmp_path):
    """DOXA runs a worker for nearly everything -- _boot, _peer_pump,
    every slash command. Textual's run_worker defaults to exit_on_error,
    so a worker death used to be indistinguishable from a DOXA crash."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _booted(app, pilot)

        async def boom() -> None:
            raise RuntimeError(f"engine handshake failed: {SECRET}")

        app.active_pane.run_worker(boom(), group="test-failure")
        await _settle(pilot)
        blocks = _blocks(app)
        assert len(blocks) == 1
        block = blocks[0]
        assert block.region.height > 0, "error block mounted at zero rows"
        assert block.region.width > 0
        assert "RuntimeError" in block.title
        assert "engine handshake failed" in block.title
        assert SECRET not in block.title
        # The app is still usable -- that is the whole claim.
        assert app.is_running
        assert app.active_pane is not None


@pytest.mark.asyncio
async def test_the_traceback_fold_opens_and_has_height(tmp_path):
    """Collapsed by default so a failure is not a wall of text, and the
    whole of it one keystroke away. A fold that expands to nothing is the
    same defect as a block that mounts at zero rows."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _booted(app, pilot)

        async def boom() -> None:
            raise ValueError("a distinctive marker string")

        app.active_pane.run_worker(boom(), group="test-failure")
        await _settle(pilot)
        block = _blocks(app)[0]
        assert block.collapsed is True
        collapsed_height = block.region.height
        block.collapsed = False
        await _settle(pilot, 20)
        assert block.region.height > collapsed_height
        assert block.body.region.height > 0, "the fold opened onto nothing"
        detail = str(block.body.renderable)
        assert "a distinctive marker string" in detail
        assert "Traceback" in detail


@pytest.mark.asyncio
async def test_a_secret_in_a_traceback_is_scrubbed_before_display(tmp_path):
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _booted(app, pilot)

        async def boom() -> None:
            raise RuntimeError(f"POST /v1/messages authorization={SECRET}")

        app.active_pane.run_worker(boom(), group="test-failure")
        await _settle(pilot)
        block = _blocks(app)[0]
        block.collapsed = False
        await _settle(pilot, 20)
        assert SECRET not in block.title
        assert SECRET not in str(block.body.renderable)
        assert "REDACTED" in str(block.body.renderable)
        # And nowhere in the persisted copy either.
        log = errors.log_path()
        assert log.exists()
        assert SECRET not in log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_repeats_collapse_into_one_block_with_a_tally(tmp_path):
    """A widget that raises while painting raises on every paint. One
    block with a count, not an unbounded column of identical blocks."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _booted(app, pilot)
        for _ in range(4):
            try:
                raise TimeoutError("the same thing again")
            except TimeoutError as exc:
                app.report_exception(exc, context="painting the screen")
        await _settle(pilot, 20)
        blocks = _blocks(app)
        assert len(blocks) == 1
        assert "×4" in blocks[0].title
        assert blocks[0].region.height > 0


@pytest.mark.asyncio
async def test_a_failure_that_never_stops_escalates_rather_than_spinning(
    tmp_path,
):
    """The backstop for a failure quarantine cannot end at its source: an
    app alive and unable to draw is not a recoverable state, so it exits
    with a report rather than repainting forever."""
    from doxa.app import FAILURE_ESCALATE

    app = DoxaApp(cwd=str(tmp_path))
    with pytest.raises(errors.FatalFailure):
        async with app.run_test(size=(100, 30)) as pilot:
            await _booted(app, pilot)
            for _ in range(FAILURE_ESCALATE + 2):
                app.report_failure(errors.policy_failure("doxa", "endless"))
            await _settle(pilot, 10)
    assert app.return_code == 1


# -- render-time containment -------------------------------------------


@pytest.mark.asyncio
async def test_a_render_raise_does_not_kill_the_app(tmp_path):
    """textual/_compositor.py has no `except` in it at all, so a widget
    that raises while rendering takes the whole FRAME with it, every
    frame. There is no per-widget render guard to hook; the containment is
    here."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        block_list = await _booted(app, pilot)
        await block_list.mount(Exploding())
        await _paint(app)
        assert app.is_running
        blocks = _blocks(app)
        assert len(blocks) == 1
        assert blocks[0].region.height > 0
        assert "TimeoutError" in blocks[0].title
        # Still usable: a fresh block mounts and paints after the raise.
        await block_list.mount(SystemBlock("still alive"))
        await _settle(pilot, 20)
        assert app.active_pane.query(SystemBlock)


@pytest.mark.asyncio
async def test_quarantine_says_what_it_hid(tmp_path):
    """Half a widget silently missing is one of the four defects this
    release is about; a WHOLE widget silently missing would be the same
    defect wearing a fix."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        block_list = await _booted(app, pilot)
        widget = Exploding()
        await block_list.mount(widget)
        await _paint(app)
        assert widget.display is False, "the culprit is still being painted"
        title = _blocks(app)[0].title
        assert "Exploding" in title
        assert "hidden" in title


@pytest.mark.asyncio
async def test_an_event_handler_raise_hides_nothing(tmp_path):
    """Quarantine is for RENDER failures only. Hiding an arbitrary widget
    because a keystroke handler threw would be a second defect, not
    containment."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        block_list = await _booted(app, pilot)
        before = [w for w in app.query(Static) if w.display]

        async def boom() -> None:
            raise RuntimeError("not a paint")

        app.active_pane.run_worker(boom(), group="test-failure")
        await _settle(pilot)
        assert _blocks(app)
        assert "hidden" not in _blocks(app)[0].title
        assert block_list.display is True
        assert all(w.display for w in before)


@pytest.mark.asyncio
async def test_the_reported_render_crash_no_longer_kills(tmp_path, monkeypatch):
    """The 2026-08-24 report, reproduced in the shape it arrived in:
    App.run() active, Textual owning stdin, a textual-image widget asking
    the terminal for its cell size while Textual is PAINTING it, and the
    read timing out.

    The raiser is rebound into ``textual_image``'s own module globals
    (``types.FunctionType`` with that module's ``__dict__``) rather than
    defined here, because attribution is half of what is being tested: a
    user must be told the terminal-image tier misbehaved, not that DOXA
    did. That is the ONE thing this test could get wrong by taking the
    easy route.

    Layering, for the record: the specific CAUSE -- textual-image probing
    stdin during a paint at all -- is fixed in doxa.images/doxa.banner on
    its own branch. This test owns the general containment: whatever the
    cause, a render raise must not be fatal.

    The boot banner is switched OFF so the only image widget on screen is
    the one mounted deliberately below -- otherwise the banner explodes
    during boot, inside ``Pilot.pause``'s unguarded refresh (see
    :func:`_paint`), and the test would be measuring the harness."""
    monkeypatch.setenv("DOXA_IMAGE_MODE", "halfblock")
    monkeypatch.setenv("DOXA_BOOT_BANNER", "0")
    import textual_image.renderable.halfcell as halfcell

    from doxa import banner, images

    def _raise_timeout(*args, **kwargs):
        raise TimeoutError("Timeout while reading terminal response")

    monkeypatch.setattr(
        halfcell,
        "get_cell_size",
        types.FunctionType(
            _raise_timeout.__code__, halfcell.__dict__, "get_cell_size",
        ),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(120, 34)) as pilot:
        block_list = await _booted(app, pilot)
        widget = images.widget_for(banner.image_source(), "doxa logo", mode="halfblock")
        assert type(widget).__name__ == "HalfcellImage", "not the real image widget"
        await block_list.mount(widget)
        await _paint(app)
        assert app.is_running, "the reported crash still kills the app"
        blocks = _blocks(app)
        assert len(blocks) == 1
        assert blocks[0].region.height > 0
        assert "TimeoutError" in blocks[0].title
        assert blocks[0].failure.origin == "textual_image", (
            "a third-party render failure is being reported as a DOXA bug"
        )
        assert widget.display is False


# -- never swallow -----------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_swallowed(tmp_path):
    """Every caught exception becomes a block. A silent `except
    Exception: pass` that now merely logs is worse than the crash it
    replaced -- the user learns nothing and the tests still pass."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _booted(app, pilot)
        assert not _blocks(app), "a clean boot produced an error block"
        assert app.failures.total() == 0
        try:
            raise KeyError("something the app caught")
        except KeyError as exc:
            app.report_exception(exc, context="doing a thing")
        await _settle(pilot, 20)
        assert len(_blocks(app)) == 1
        assert app.failures.total() == 1
        assert errors.log_path().exists()


@pytest.mark.asyncio
async def test_a_failed_answer_delivery_is_reported_and_says_what_to_do(
    tmp_path,
):
    """Defect two of four: the needs-input dialog "stopped answering
    keys". The popup had already closed and the flag had already cleared,
    so a failed delivery left the agent blocked forever on a question the
    user HAD answered, with no dialog and no message."""
    from doxa.ui.dialogs import NeedsInputPopup

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _booted(app, pilot)
        pane = app.active_pane

        async def _refuse(request_id, answer):
            raise ConnectionResetError("daemon socket closed")

        pane.engine.answer_needs_input = _refuse  # type: ignore[union-attr]
        popup = pane.query_one("#needs-input-popup", NeedsInputPopup)
        popup.ask({
            "id": "req-1",
            "kind": "permission",
            "tool_name": "Bash",
            "input_summary": "Bash ls",
            "title": "Claude wants to run ls",
        })
        assert popup.is_open
        await pane._resolve_needs_input(popup, None, True)
        await _settle(pilot, 20)
        blocks = _blocks(app)
        assert len(blocks) == 1
        assert "ConnectionResetError" in blocks[0].title
        assert blocks[0].region.height > 0
        told = [
            str(b.renderable) for b in pane.query(SystemBlock)
            if "did not reach the session" in str(b.renderable)
        ]
        assert told, "the session wedged silently again"


# -- ctrl+c is not a defect --------------------------------------------


def test_the_boundary_cannot_receive_ctrl_c_or_a_deliberate_exit():
    """KeyboardInterrupt and SystemExit derive from BaseException, so
    Textual's own `except Exception` clauses cannot catch them and this
    signature cannot receive them. Ctrl+C behaviour is bound in app.py and
    must keep working."""
    assert not issubclass(KeyboardInterrupt, Exception)
    assert not issubclass(SystemExit, Exception)


@pytest.mark.asyncio
async def test_ctrl_c_still_arms_the_double_press_window(tmp_path):
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _booted(app, pilot)
        await app.action_ctrl_c_quit()
        assert app._ctrl_c_timer is not None
        assert not _blocks(app), "a quit keystroke was reported as a failure"
        app._ctrl_c_timer.stop()
        app._ctrl_c_timer = None


# -- no per-frame cost -------------------------------------------------


@pytest.mark.asyncio
async def test_the_error_surface_arms_no_timer(tmp_path):
    """The no-timer discipline (GitLine's docstring, _refresh_status's
    note on the idle-CPU regression this app already paid to shed). This
    feature does work only when something has already gone wrong."""
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        block_list = await _booted(app, pilot)

        async def boom() -> None:
            raise RuntimeError("armed anything?")

        app.active_pane.run_worker(boom(), group="test-failure")
        await _settle(pilot)
        block = _blocks(app)[0]
        assert block.auto_refresh is None
        assert block.body.auto_refresh is None
        assert block_list.auto_refresh is None
