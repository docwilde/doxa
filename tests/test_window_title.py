"""The terminal's own window/taskbar title (v0.57.0).

Two halves, and the second is the one worth having tests for: DOXA sets
the title, and DOXA GIVES IT BACK. A terminal left titled "DOXA" after the
app is gone is a mess the user cannot clean up -- they do not know the
escape sequence either -- so "restored on exit" is asserted on the clean
path, the crash path and the Ctrl+C path separately.

Everything here drives a fake tty rather than the real stdout: the suite
has no terminal, and a test that let real escapes reach the captured
output would be measuring pytest's capture rather than DOXA.
"""

from __future__ import annotations

import io

import pytest

from doxa import window


class FakeTTY(io.StringIO):
    """A stream that claims to be a terminal, so :func:`window.supported`
    says yes and the real ``_emit`` runs."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv(window.OPT_OUT_ENV, raising=False)
    return FakeTTY()


# -- what the title says ------------------------------------------------


def test_title_is_doxa_plus_the_project(tmp_path):
    """The floor the user asked for is "DOXA"; the project is what makes
    two DOXA windows tellable apart on a taskbar, which is the whole job
    of the string."""
    project = tmp_path / "ampiric"
    project.mkdir()
    assert window.title_for(project) == "DOXA — ampiric"


def test_title_is_bare_doxa_with_no_cwd():
    assert window.title_for(None) == "DOXA"
    assert window.title_for("") == "DOXA"


def test_title_is_bare_doxa_at_the_filesystem_root():
    assert window.title_for("/") == "DOXA"


def test_a_control_character_in_a_directory_name_cannot_escape_the_sequence(tty):
    """A directory name is attacker-influenced on a machine that clones
    repositories. A BEL inside the payload would terminate the OSC early
    and spray the rest across the screen."""
    window.set_title("evil\x07rm -rf /\nmore", tty)
    written = tty.getvalue()
    assert written.startswith("\x1b]0;")
    assert written.endswith("\x07")
    assert written.count("\x07") == 1
    assert "\n" not in written


def test_a_very_long_project_name_is_bounded(tmp_path):
    project = tmp_path / ("x" * 200)  # under the filesystem's 255, over ours
    project.mkdir()
    assert len(window.title_for(project)) <= len("DOXA — ") + window._MAX_TITLE_LEN


# -- when DOXA refuses to touch the title -------------------------------


def test_nothing_is_written_when_the_stream_is_not_a_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    plain = io.StringIO()  # isatty() -> False
    with window.terminal_title("DOXA", plain):
        pass
    assert plain.getvalue() == ""


def test_nothing_is_written_under_a_dumb_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    stream = FakeTTY()
    with window.terminal_title("DOXA", stream):
        pass
    assert stream.getvalue() == ""


def test_the_opt_out_env_var_stops_everything(monkeypatch, tty):
    monkeypatch.setenv(window.OPT_OUT_ENV, "1")
    with window.terminal_title("DOXA", tty):
        pass
    assert tty.getvalue() == ""


# -- set, and give it back ----------------------------------------------


def test_the_title_is_pushed_set_and_popped_in_that_order(tty):
    """Push BEFORE set: the terminal's title stack has to capture the
    user's own title while it is still the one on screen. Pop after."""
    with window.terminal_title("DOXA — repo", tty):
        assert tty.getvalue() == window._PUSH + "\x1b]0;DOXA — repo\x07"
    assert tty.getvalue() == (
        window._PUSH + "\x1b]0;DOXA — repo\x07" + window._POP
    )


def test_the_title_is_restored_when_the_block_raises(tty):
    """A crash must not cost the user their window title."""
    with pytest.raises(RuntimeError):
        with window.terminal_title("DOXA", tty):
            raise RuntimeError("boom")
    assert tty.getvalue().endswith(window._POP)


def test_the_title_is_restored_on_keyboard_interrupt(tty):
    """Ctrl+C before the app's own binding is live raises
    KeyboardInterrupt straight out of run(); it inherits from
    BaseException, so a bare `except Exception` would have missed it and
    `finally` is why this holds."""
    with pytest.raises(KeyboardInterrupt):
        with window.terminal_title("DOXA", tty):
            raise KeyboardInterrupt
    assert tty.getvalue().endswith(window._POP)


def test_a_stream_that_dies_mid_block_does_not_break_the_exit(tty):
    """The restore is best-effort by construction: a closed stream at exit
    must not turn a clean quit into a traceback."""
    with window.terminal_title("DOXA", tty):
        tty.close()
    # No exception is the assertion.


# -- the app seam -------------------------------------------------------


@pytest.mark.parametrize("blow_up", [False, True])
def test_doxa_app_run_owns_and_returns_the_title(monkeypatch, tmp_path, blow_up):
    """DoxaApp.run() is the ONE door every entry point goes through --
    `doxa new`, `doxa attach`, a tabset restore and `--in-process` -- so
    wrapping it there is what stops the next entry point shipping without
    the restore. Asserted on both the clean return and the crash."""
    from textual.app import App

    from doxa.app import DoxaApp

    emitted: list[str] = []
    monkeypatch.setattr(window, "supported", lambda stream=None: True)
    monkeypatch.setattr(window, "_emit", lambda text, stream: emitted.append(text))

    def fake_run(self, *args, **kwargs):
        assert emitted == [window._PUSH, f"\x1b]0;DOXA — {tmp_path.name}\x07"], (
            "the title must already be set while the app is running"
        )
        if blow_up:
            raise RuntimeError("the app fell over")
        return "clean"

    monkeypatch.setattr(App, "run", fake_run)
    app = DoxaApp(cwd=str(tmp_path))
    if blow_up:
        with pytest.raises(RuntimeError):
            app.run()
    else:
        assert app.run() == "clean"
    assert emitted[-1] == window._POP
