# SPDX-License-Identifier: AGPL-3.0-only
"""Item O (keyboard-protocol detection): which key combinations this
terminal can physically send, and saying so where a user looks.

The failure this fixes has no error message. DOXA binds ``ctrl+comma`` to
/settings; a terminal speaking the legacy key encoding cannot produce a
byte for Ctrl+, at all, so the documented key does nothing, forever,
silently -- and nothing anywhere told the user whether DOXA or the
terminal was at fault.

Three things are asserted here, and the third is the one worth stating up
front:

1. The predicate. :func:`doxa.keyboard.unreachable_under_legacy` is a
   truth table against the legacy encoding, including the cases it must
   NOT claim -- modified cursor keys, Alt+Enter, Shift+Tab -- because a
   wrong "this key won't work" sends a user into their terminal settings
   after a bug that is ours.
2. The probe. A real pty, a real escape handshake, a thread playing the
   terminal. The Primary-DA sentinel is what makes silence readable:
   a terminal that answers DA and not the ``u`` query is measurably
   legacy; one that answers nothing at all is measurably nothing, and
   reports "unknown".
3. The degradation. Headless -- the suite's own condition -- the probe
   writes no byte, raises nothing, and every surface says "not measured"
   rather than claiming a protocol it never observed.
"""

from __future__ import annotations

import os
import select
import sys
import threading
import time

import pytest

from doxa import doctor as doctor_mod
from doxa import keyboard as keyboard_mod
from doxa import version as version_mod
from doxa.app import AboutDialog, DoxaApp
from doxa.ui.labels import UNREACHABLE_MARK, help_text, unreachable_bindings
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts with an unsettled probe. The cache is module
    state by design (the probe may only run while this process owns the
    terminal), so a test that leaves it warm poisons the next one."""
    keyboard_mod._detected = None
    yield
    keyboard_mod._detected = None


# -- the predicate -----------------------------------------------------


@pytest.mark.parametrize("key", [
    # No C0 code exists for Ctrl+<punctuation that isn't @ [ \ ] ^ _ ?>.
    # This is DOXA's own /settings binding.
    "ctrl+comma",
    "ctrl+full_stop",
    "ctrl+minus",
    "ctrl+1",
    # The byte a legacy terminal sends for these IS another key's byte,
    # so Textual dispatches that other key and the binding never fires.
    "ctrl+i",
    "ctrl+m",
    "ctrl+h",
    "ctrl+left_square_bracket",
    # Enter/Tab/Escape/Backspace carry no modifier bits at all.
    "shift+enter",
    "ctrl+enter",
    "ctrl+tab",
    "shift+backspace",
    "ctrl+space",
    # Ctrl+Shift+<letter> is the same byte as Ctrl+<letter>.
    "ctrl+shift+a",
    # Modifiers the encoding cannot express in any position.
    "super+k",
    "hyper+left",
    # Alt+<CHARACTER>. Corrected in v0.95.0 after the live report that
    # Alt+S and Alt+D did nothing. The old entry reasoned about the
    # TERMINAL (which does send Alt, as an ESC prefix, and always has)
    # when the binding depends on what TEXTUAL decodes -- and textual
    # 5.3.0 has no ESC-prefix-to-Alt path at all. Measured:
    # XTermParser().feed("\x1bs") -> Key('escape'), Key('s'), so a
    # binding on alt+s never fires and the bare letter lands in the
    # prompt instead. See test_textual_cannot_decode_alt_from_an_esc_prefix
    # in tests/test_split_keys.py, which pins the parser itself.
    "alt+s", "alt+d", "alt+g", "alt+x", "alt+comma", "alt+enter",
    "alt+tab", "alt+backspace", "alt+space",
    # Ctrl+Alt is an ESC prefix in front of the C0 byte -- no better.
    "ctrl+alt+x",
])
def test_known_unreachable_combinations_are_reported_as_such(key):
    assert keyboard_mod.unreachable_under_legacy(key) is True, key


@pytest.mark.parametrize("key", [
    # C0 controls: every one of DOXA's other app bindings.
    "ctrl+p", "ctrl+r", "ctrl+t", "ctrl+w", "ctrl+q", "ctrl+c", "ctrl+g",
    "ctrl+j",  # LF, which Textual maps to ctrl+j and nothing else
    "ctrl+underscore", "ctrl+backslash", "ctrl+at",
    # Modified cursor and function keys: xterm has encoded these as
    # CSI 1;<mods><final> since long before the kitty protocol existed.
    "ctrl+left", "ctrl+right", "shift+up", "ctrl+shift+left", "alt+f4",
    "ctrl+home", "shift+delete", "ctrl+pageup",
    # ...INCLUDING under Alt, which is the whole reason alt+arrow kept
    # its binding in v0.95.0 while alt+<letter> lost its. A modified
    # ARROW is CSI 1;3<final>, a different physical encoding from the ESC
    # prefix a modified LETTER uses, and Textual decodes it:
    # XTermParser().feed("\x1b[1;3D") -> Key('alt+left').
    "alt+left", "alt+right", "alt+up", "alt+down", "alt+f4",
    # Back-tab: the one modified form the legacy encoding does carry.
    "shift+tab",
    # Unmodified anything.
    "enter", "escape", "a", "comma", "f5", "left",
])
def test_reachable_combinations_are_never_claimed_unreachable(key):
    assert keyboard_mod.unreachable_under_legacy(key) is False, key


def test_the_predicate_is_silent_about_what_it_has_not_been_taught():
    """The asymmetry is the design: an unrecognised key name is assumed
    reachable, so a binding this module has never heard of is never
    labelled dead."""
    assert keyboard_mod.unreachable_under_legacy("ctrl+some_future_key") is False
    assert keyboard_mod.unreachable_under_legacy("") is False


# -- the probe ---------------------------------------------------------


class _FakeTerminal:
    """A real pty with a thread on the far end playing the terminal.

    The probe writes its queries at what it believes is a terminal and
    blocks in ``select`` for a reply, so nothing short of a pty exercises
    the handshake it actually performs -- raw mode included."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.master, self._slave = os.openpty()
        self.stdin = os.fdopen(self._slave, "rb", buffering=0)
        self.stdout = os.fdopen(os.dup(self._slave), "w")
        self.saw = ""
        self._thread = threading.Thread(target=self._respond, daemon=True)
        self._thread.start()

    def _respond(self) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ready, _w, _x = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 256)
            except OSError:
                return
            if not chunk:
                return
            self.saw += chunk.decode("ascii", "replace")
            # Both queries have landed; a real terminal answers now.
            if "\x1b[c" in self.saw:
                if self.reply:
                    try:
                        os.write(self.master, self.reply.encode("ascii"))
                    except OSError:
                        pass
                return

    def close(self) -> None:
        self._thread.join(timeout=3.0)
        for handle in (self.stdin, self.stdout):
            try:
                handle.close()
            except OSError:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


def _probe_against(monkeypatch, reply: str) -> tuple[str, str]:
    terminal = _FakeTerminal(reply)
    monkeypatch.setattr(sys, "__stdin__", terminal.stdin)
    monkeypatch.setattr(sys, "__stdout__", terminal.stdout)
    try:
        result = keyboard_mod._probe()
    finally:
        terminal.close()
    return result, terminal.saw


def test_a_terminal_that_answers_the_kitty_query_is_measured_as_kitty(monkeypatch):
    result, saw = _probe_against(monkeypatch, "\x1b[?0u\x1b[?62;4c")
    assert result == keyboard_mod.KITTY
    # It really did ask, and asked in the documented order.
    assert saw == keyboard_mod.QUERY


def test_flags_already_set_still_read_as_kitty(monkeypatch):
    """The reply carries whichever progressive-enhancement flags happen to
    be pushed at probe time. DOXA cares that the terminal ANSWERED, not
    what it answered with -- Textual pushes its own flag afterwards."""
    result, _saw = _probe_against(monkeypatch, "\x1b[?15u\x1b[?1;2c")
    assert result == keyboard_mod.KITTY


def test_a_terminal_that_answers_only_device_attributes_is_measured_as_legacy(
    monkeypatch,
):
    """The sentinel doing its job: the terminal was listening, it replied,
    and it had nothing to say about the keyboard protocol."""
    result, _saw = _probe_against(monkeypatch, "\x1b[?62;1;6c")
    assert result == keyboard_mod.LEGACY


def test_silence_is_unknown_not_legacy(monkeypatch):
    """The whole honesty argument in one assertion. No reply means nobody
    was listening on our behalf -- Textual's reader thread already owns
    stdin, or the terminal is slow -- which says NOTHING about the
    keyboard, so we say nothing about it."""
    result, _saw = _probe_against(monkeypatch, "")
    assert result == keyboard_mod.UNKNOWN


def test_a_reply_that_is_only_the_kitty_answer_is_still_unknown(monkeypatch):
    """Belt and braces: without the DA sentinel we cannot know the reply
    is complete, so a truncated conversation does not become a claim."""
    result, _saw = _probe_against(monkeypatch, "\x1b[?0u")
    assert result == keyboard_mod.UNKNOWN


def test_the_probe_leaves_the_terminal_as_it_found_it(monkeypatch):
    """Raw mode is set for the handshake and restored afterwards --
    a probe that leaked it would leave the user's shell without echo."""
    import termios

    terminal = _FakeTerminal("\x1b[?0u\x1b[?62c")
    monkeypatch.setattr(sys, "__stdin__", terminal.stdin)
    monkeypatch.setattr(sys, "__stdout__", terminal.stdout)
    fd = terminal.stdin.fileno()
    before = termios.tcgetattr(fd)
    try:
        assert keyboard_mod._probe() == keyboard_mod.KITTY
        assert termios.tcgetattr(fd) == before
    finally:
        terminal.close()


# -- headless degradation ----------------------------------------------


def test_headless_detection_writes_nothing_and_raises_nothing(monkeypatch):
    """The suite's own condition, and CI's. ``_is_tty`` is False, so the
    probe short-circuits before touching stdin or stdout at all."""
    monkeypatch.delenv(keyboard_mod.ENV_VAR, raising=False)
    monkeypatch.setattr(keyboard_mod, "_is_tty", lambda: False)

    def _explode() -> str:
        raise AssertionError("the probe talked to a terminal that isn't there")

    monkeypatch.setattr(keyboard_mod, "_ask_terminal", _explode)
    assert keyboard_mod.detect_protocol() == keyboard_mod.UNKNOWN


def test_a_probe_that_explodes_is_ignorance_not_a_crash(monkeypatch):
    """No termios (Windows), a terminal that hangs up mid-read: none of
    them may take a session down, and none of them may produce an answer."""
    monkeypatch.delenv(keyboard_mod.ENV_VAR, raising=False)
    monkeypatch.setattr(keyboard_mod, "_is_tty", lambda: True)

    def _boom() -> str:
        raise OSError("no such device")

    monkeypatch.setattr(keyboard_mod, "_ask_terminal", _boom)
    assert keyboard_mod.detect_protocol() == keyboard_mod.UNKNOWN


def test_the_probe_runs_at_most_once(monkeypatch):
    """It may only run while this process owns the terminal, so the cache
    is a correctness property, not an optimisation."""
    monkeypatch.delenv(keyboard_mod.ENV_VAR, raising=False)
    calls: list[int] = []

    def _once() -> str:
        calls.append(1)
        return keyboard_mod.LEGACY

    monkeypatch.setattr(keyboard_mod, "_probe", _once)
    assert keyboard_mod.detect_protocol() == keyboard_mod.LEGACY
    assert keyboard_mod.detect_protocol() == keyboard_mod.LEGACY
    assert len(calls) == 1


def test_the_env_override_wins_and_a_typo_degrades_to_detection(monkeypatch):
    monkeypatch.setattr(keyboard_mod, "_probe", lambda: keyboard_mod.UNKNOWN)
    monkeypatch.setenv(keyboard_mod.ENV_VAR, "kitty")
    assert keyboard_mod.detect_protocol() == keyboard_mod.KITTY
    monkeypatch.setenv(keyboard_mod.ENV_VAR, "KITTY")
    assert keyboard_mod.detect_protocol() == keyboard_mod.KITTY
    monkeypatch.setenv(keyboard_mod.ENV_VAR, "kitteh")
    assert keyboard_mod.detect_protocol() == keyboard_mod.UNKNOWN


def test_nothing_is_marked_unreachable_on_an_unmeasured_terminal(monkeypatch):
    """The rule that makes the annotation safe to ship: is_unreachable is
    False for a key that IS unreachable under legacy, whenever we have not
    established that this terminal is legacy."""
    assert keyboard_mod.unreachable_under_legacy("ctrl+comma") is True
    for protocol in (keyboard_mod.UNKNOWN, keyboard_mod.KITTY):
        monkeypatch.setenv(keyboard_mod.ENV_VAR, protocol)
        assert keyboard_mod.is_unreachable("ctrl+comma") is False
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    assert keyboard_mod.is_unreachable("ctrl+comma") is True


# -- /about ------------------------------------------------------------


def test_about_has_a_keyboard_row_in_every_state(monkeypatch):
    for protocol, expected in (
        (keyboard_mod.KITTY, "kitty"),
        (keyboard_mod.LEGACY, "legacy"),
        (keyboard_mod.UNKNOWN, "not measured"),
    ):
        monkeypatch.setenv(keyboard_mod.ENV_VAR, protocol)
        rows = dict(version_mod.about_rows())
        assert "keyboard" in rows, f"/about has no keyboard row ({protocol})"
        assert expected in rows["keyboard"], rows["keyboard"]


def test_the_keyboard_row_reaches_the_copyable_text(monkeypatch):
    """/about's body and its copy door are one builder; a row that only
    existed in the list would never reach the issue someone files."""
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    text = version_mod.about_text()
    assert "keyboard" in text
    assert dict(version_mod.about_rows())["keyboard"] in text


@pytest.mark.asyncio
async def test_the_about_dialog_actually_draws_the_keyboard_row(
    monkeypatch, tmp_path,
):
    """Rendered, not merely present in a list: real height, real width,
    and the row's own text on the widget that drew. The v0.28.0 defect
    (a modal laid out at zero height passing every query_one in the
    suite) is why this dialog's tests measure geometry."""
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path) + "-rt")

    def make():
        return FakeEngine([])

    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
        new_session_factory_at=lambda path: make(),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(AboutDialog())
        for _ in range(200):
            if isinstance(app.screen, AboutDialog):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, AboutDialog)
        await pilot.pause()

        body = app.screen.query_one("#about-body")
        assert body.size.height > 0, f"about body collapsed: {body.size}"
        assert body.size.width > 0, f"about body collapsed: {body.size}"
        rendered = str(body.renderable)
        assert "keyboard" in rendered
        assert "legacy" in rendered


# -- /help -------------------------------------------------------------


def test_help_marks_a_binding_this_terminal_cannot_send(monkeypatch):
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    text = help_text()
    # /settings is bound to ctrl+comma, which no legacy terminal can send.
    assert f"[Ctrl+,]{UNREACHABLE_MARK}" in text
    # And the mark is explained where it appears, not left as a glyph.
    assert "cannot send that combination" in text
    assert "kitty keyboard protocol" in text


def test_help_marks_nothing_when_the_terminal_can_send_everything(monkeypatch):
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.KITTY)
    text = help_text()
    assert "[Ctrl+,]" in text
    assert "✗" not in text
    assert "cannot send that combination" not in text


def test_help_marks_nothing_when_the_protocol_was_not_measured(monkeypatch):
    """Headless, or any terminal we could not ask: /help is byte-identical
    to a DOXA that never had this feature. A false "this key is dead" is
    worse than no annotation."""
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.UNKNOWN)
    text = help_text()
    assert "✗" not in text
    assert "cannot send that combination" not in text


def test_help_still_lists_the_binding_it_marks(monkeypatch):
    """Item O reports; it does not re-map. The binding is still there, in
    its group, with its slash command beside it -- which is the escape
    route the footnote points at."""
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    text = help_text()
    assert "/settings" in text
    assert "[Ctrl+,]" in text
    # Every other app binding is a C0 control and stays unmarked.
    for pretty in ("[Ctrl+R]", "Ctrl+P", "Ctrl+T", "Ctrl+W", "Ctrl+Q", "Ctrl+←"):
        assert f"{pretty}✗" not in text


def test_unreachable_bindings_names_the_real_ones(monkeypatch):
    # Ctrl+Tab joined this list in v0.42.0 and is SUPPOSED to be in it.
    # The operator asked for Ctrl+Tab as the permission-mode cycle key;
    # this module's own predicate says a legacy terminal cannot send it,
    # so Shift+Tab (which it CAN send -- back-tab, CSI Z) is the primary
    # binding and Ctrl+Tab rides beside it for terminals speaking the
    # kitty protocol. Appearing here is the whole deal: a second, partly
    # deliverable binding is only defensible because /help and /doctor say
    # out loud where it does not work, instead of leaving it documented
    # and silently dead -- which is the defect v0.39.0 exists to close.
    #
    # Alt+S / Alt+D / Alt+G are here as of v0.95.0, and the previous
    # version of this comment claimed the opposite in so many words:
    # "Alt is an ESC prefix every terminal has sent since long before the
    # kitty protocol, so both are reachable under either encoding". The
    # terminal half is true; the conclusion was not, because Textual has
    # no ESC-prefix-to-Alt path and hands the app Escape-then-letter. The
    # keys stayed BOUND (real muscle memory on kitty/ghostty/WezTerm/foot)
    # and moved off the primary slot -- Ctrl+O, Ctrl+N and F2 carry that
    # now -- so appearing in this list is again the whole deal: a second,
    # partly deliverable binding is defensible only while /help and
    # /doctor say out loud where it does not work.
    #
    # The pane-movement keys (Ctrl+Shift+arrow) and the between-leaf
    # divider (Alt+ARROW) are still absent, and for the reason the split
    # keys turned out not to have: modified cursor keys go out as
    # CSI 1;<mods><final> sequences every terminal since xterm sends, and
    # Textual decodes those under both protocols.
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    assert unreachable_bindings() == [
        "Ctrl+,", "Ctrl+Tab", "Alt+S", "Alt+D", "Alt+G",
    ]
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.KITTY)
    assert unreachable_bindings() == []
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.UNKNOWN)
    assert unreachable_bindings() == []


# -- /doctor -----------------------------------------------------------


def test_doctor_reports_the_measured_protocol(monkeypatch):
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.KITTY)
    check = doctor_mod._keyboard_enhancement_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "kitty" in check.detail

    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    check = doctor_mod._keyboard_enhancement_check()
    # A legacy terminal is not a broken install: `doxa doctor` must not
    # exit non-zero on a machine where nothing is wrong.
    assert check.status == doctor_mod.STATUS_PASS
    assert "legacy" in check.detail
    assert "Ctrl+," in check.detail


def test_doctor_still_says_unknown_when_nothing_was_measured(monkeypatch):
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.UNKNOWN)
    check = doctor_mod._keyboard_enhancement_check()
    assert check.status == doctor_mod.STATUS_UNKNOWN
    assert "not measured" in check.detail


def test_a_legacy_terminal_does_not_fail_the_doctor(monkeypatch):
    monkeypatch.setenv(keyboard_mod.ENV_VAR, keyboard_mod.LEGACY)
    checks = [doctor_mod._keyboard_enhancement_check()]
    assert doctor_mod.any_failing(checks) is False
