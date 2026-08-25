# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.notify -- the gating matrix (master mode x per-trigger bool x
focus), the notify-send call itself, and the LORE_NOTIFY inheritance
bridge.

Every ``notify-send`` invocation is a scripted stand-in (monkeypatched
``shutil.which``/``subprocess.run``) -- no test here may pop a real desktop
notification, on this machine or CI's.
"""

from __future__ import annotations

import subprocess

import pytest

from doxa import config, notify as notify_mod


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config.invalidate()
    yield
    config.invalidate()


@pytest.fixture(autouse=True)
def _reset_lore_notify_latch(monkeypatch):
    """sync_lore_notify_env's "did we set it" flag is module-global state --
    every test starts and ends with a clean slate, and LORE_NOTIFY itself
    is never left behind for a later test to trip over."""
    monkeypatch.setattr(notify_mod, "_lore_notify_silenced_by_us", False)
    monkeypatch.delenv("LORE_NOTIFY", raising=False)
    yield
    monkeypatch.delenv("LORE_NOTIFY", raising=False)


# -- the gating matrix ------------------------------------------------------


@pytest.mark.parametrize(
    "mode,focused,expected",
    [
        ("off", True, False),
        ("off", False, False),
        ("auto", True, False),   # focused: auto stays quiet
        ("auto", False, True),   # unfocused: auto speaks
        ("always", True, True),  # always ignores focus
        ("always", False, True),
    ],
)
def test_gating_matrix(monkeypatch, mode, focused, expected):
    config.save({"notify": mode})
    assert notify_mod.should_fire("DOXA_NOTIFY_TURN_DONE", focused) is expected


def test_a_disabled_trigger_never_fires_regardless_of_mode_or_focus():
    config.save({"notify": "always", "notify_turn_done": "0"})
    assert notify_mod.should_fire("DOXA_NOTIFY_TURN_DONE", False) is False


def test_an_unrecognised_mode_degrades_to_auto(monkeypatch):
    monkeypatch.setenv("DOXA_NOTIFY", "sometimes")
    assert notify_mod.should_fire("DOXA_NOTIFY_TURN_DONE", True) is False
    assert notify_mod.should_fire("DOXA_NOTIFY_TURN_DONE", False) is True


def test_env_beats_config_for_the_master_mode(monkeypatch):
    config.save({"notify": "off"})
    monkeypatch.setenv("DOXA_NOTIFY", "always")
    assert notify_mod.should_fire("DOXA_NOTIFY_TURN_DONE", True) is True


# -- notify() itself ---------------------------------------------------------


def test_notify_is_a_silent_no_op_when_notify_send_is_missing(monkeypatch):
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(
        notify_mod.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )
    notify_mod.notify("title", "body")
    assert calls == []


def test_notify_shells_out_to_notify_send_with_no_shell(monkeypatch):
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: "/usr/bin/notify-send")
    calls = []

    def fake_run(argv, timeout=None, check=None, capture_output=None):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(notify_mod.subprocess, "run", fake_run)
    notify_mod.notify("hello", "world")
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "/usr/bin/notify-send"
    assert argv[-2:] == ["hello", "world"]
    assert "-a" in argv and "doxa" in argv


def test_notify_never_raises_when_the_subprocess_call_fails(monkeypatch):
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: "/usr/bin/notify-send")

    def boom(*a, **k):
        raise OSError("no such device")

    monkeypatch.setattr(notify_mod.subprocess, "run", boom)
    notify_mod.notify("title", "body")  # must not raise


def test_notify_icon_from_env_theme_name_passes_through(monkeypatch):
    monkeypatch.setenv("DOXA_NOTIFY_ICON", "dialog-information")
    assert notify_mod.notify_icon() == "dialog-information"


def test_notify_icon_from_env_path_only_when_the_file_exists(monkeypatch, tmp_path):
    missing = tmp_path / "nope.svg"
    monkeypatch.setenv("DOXA_NOTIFY_ICON", str(missing))
    assert notify_mod.notify_icon() is None
    missing.write_text("x", encoding="utf-8")
    assert notify_mod.notify_icon() == str(missing)


def test_notify_carries_the_icon_flag_when_one_resolves(monkeypatch):
    monkeypatch.setenv("DOXA_NOTIFY_ICON", "dialog-information")
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: "/usr/bin/notify-send")
    calls = []
    monkeypatch.setattr(
        notify_mod.subprocess, "run",
        lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    notify_mod.notify("t", "b")
    assert "-i" in calls[0] and "dialog-information" in calls[0]


# -- the wired triggers -------------------------------------------------


def test_notify_turn_done_includes_duration_when_given(monkeypatch):
    config.save({"notify": "always"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_turn_done(True, "Sonnet@doxa:main", 1500)
    assert calls == [("Sonnet@doxa:main", "response finished (1.5s)")]


def test_notify_turn_done_omits_duration_when_absent(monkeypatch):
    config.save({"notify": "always"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_turn_done(True, "Sonnet@doxa:main", None)
    assert calls == [("Sonnet@doxa:main", "response finished")]


def test_notify_turn_done_is_gated_like_any_other_trigger(monkeypatch):
    config.save({"notify": "auto"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_turn_done(True, "tab", 100)  # focused, auto: silent
    assert calls == []
    notify_mod.notify_turn_done(False, "tab", 100)  # unfocused: speaks
    assert len(calls) == 1


def test_notify_needs_input_includes_the_summary(monkeypatch):
    config.save({"notify": "always"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_needs_input(True, "Sonnet@doxa:main", "which environment?")
    assert calls == [("Sonnet@doxa:main", "which environment?")]


def test_notify_needs_input_falls_back_when_summary_is_empty(monkeypatch):
    config.save({"notify": "always"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_needs_input(True, "Sonnet@doxa:main", "")
    assert calls == [("Sonnet@doxa:main", "needs your input")]


def test_notify_needs_input_truncates_a_long_summary(monkeypatch):
    config.save({"notify": "always"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_needs_input(True, "tab", "x" * 200)
    body = calls[0][1]
    assert len(body) == 121  # 120 chars + the ellipsis
    assert body.endswith("…")


def test_notify_needs_input_is_gated_like_any_other_trigger(monkeypatch):
    config.save({"notify": "auto"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_needs_input(True, "tab", "q")  # focused, auto: silent
    assert calls == []
    notify_mod.notify_needs_input(False, "tab", "q")  # unfocused: speaks
    assert len(calls) == 1


def test_notify_needs_input_toggle_off_silences_it_even_when_unfocused(monkeypatch):
    config.save({"notify": "always", "notify_needs_input": "0"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_needs_input(False, "tab", "q")
    assert calls == []


def test_notify_update_available_message(monkeypatch):
    config.save({"notify": "always"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_update_available(False)
    assert calls == [("DOXA update available", "/update")]


def test_per_trigger_bools_are_independent(monkeypatch):
    config.save({"notify": "always", "notify_update": "0"})
    calls = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body: calls.append((title, body)))
    notify_mod.notify_update_available(False)
    assert calls == []  # this trigger is off
    notify_mod.notify_turn_done(False, "tab", None)
    assert len(calls) == 1  # unrelated trigger, untouched


# -- LORE inheritance: notify_lore / notify_staged <-> LORE_NOTIFY ---------
#
# v0.31.0 gave DOXA its OWN staged-proposal banner (notify_staged), which
# knows about window focus and carries the proposal text -- things
# lore_core's blunt LORE_NOTIFY banner cannot do. Two notifiers for one
# event would mean two banners, so while DOXA's is on, lore_core's is held
# silent. The pre-v0.31.0 rule (notify_lore alone decides) is what is left
# once DOXA's own trigger is off, so turning it off never leaves the user
# with silence they did not ask for.


def test_notify_lore_off_sets_lore_notify_zero():
    import os

    config.save({"notify_lore": "0", "notify_staged": "0"})
    notify_mod.sync_lore_notify_env()
    assert os.environ["LORE_NOTIFY"] == "0"


def test_notify_lore_on_leaves_an_unset_var_alone():
    config.save({"notify_lore": "1", "notify_staged": "0"})
    notify_mod.sync_lore_notify_env()
    import os

    assert "LORE_NOTIFY" not in os.environ


def test_turning_notify_lore_back_on_undoes_our_own_override():
    import os

    config.save({"notify_lore": "0", "notify_staged": "0"})
    notify_mod.sync_lore_notify_env()
    assert os.environ["LORE_NOTIFY"] == "0"

    config.save({"notify_lore": "1", "notify_staged": "0"})
    notify_mod.sync_lore_notify_env()
    assert "LORE_NOTIFY" not in os.environ


def test_a_users_own_lore_notify_choice_survives_notify_lore_on(monkeypatch):
    """We never set LORE_NOTIFY=0 -> notify_lore=on must not pop a value
    that was NOT ours to begin with."""
    monkeypatch.setenv("LORE_NOTIFY", "0")
    config.save({"notify_lore": "1", "notify_staged": "0"})
    notify_mod.sync_lore_notify_env()
    import os

    assert os.environ["LORE_NOTIFY"] == "0"  # untouched: not ours


def test_doxas_own_staged_banner_silences_lore_cores_duplicate():
    """The default state, and the whole point: notify_staged is ON out of
    the box, so one staged batch produces ONE notification -- DOXA's
    focus-gated one -- rather than DOXA's plus lore_core's."""
    import os

    config.save({"notify_lore": "1", "notify_staged": "1"})
    notify_mod.sync_lore_notify_env()
    assert os.environ["LORE_NOTIFY"] == "0"


def test_turning_doxas_staged_banner_off_hands_the_job_back():
    import os

    config.save({"notify_lore": "1", "notify_staged": "1"})
    notify_mod.sync_lore_notify_env()
    assert os.environ["LORE_NOTIFY"] == "0"

    config.save({"notify_staged": "0"})
    notify_mod.sync_lore_notify_env()
    assert "LORE_NOTIFY" not in os.environ


# -- the staged trigger itself ---------------------------------------------


def _script_notify_send(monkeypatch) -> list:
    """Collect (title, body) instead of popping a real banner -- the same
    ``notify_mod.notify`` stand-in every trigger test above installs."""
    calls: list = []
    monkeypatch.setattr(
        notify_mod, "notify", lambda title, body: calls.append((title, body))
    )
    return calls


def test_notify_staged_names_the_tab_and_quotes_the_first_proposal(monkeypatch):
    sent = _script_notify_send(monkeypatch)
    config.save({"notify": "always"})
    notify_mod.notify_staged(True, "doxa · repo", 3, ["remember uv, not pip"])
    assert sent[0][-2] == "doxa · repo"
    body = sent[0][-1]
    assert "3 proposals staged" in body
    assert "remember uv, not pip" in body


def test_notify_staged_says_one_proposal_in_the_singular(monkeypatch):
    sent = _script_notify_send(monkeypatch)
    config.save({"notify": "always"})
    notify_mod.notify_staged(True, "tab", 1, [])
    assert "1 proposal staged" in sent[0][-1]


def test_notify_staged_survives_an_empty_preview(monkeypatch):
    sent = _script_notify_send(monkeypatch)
    config.save({"notify": "always"})
    notify_mod.notify_staged(True, "tab", 2, None)
    assert "2 proposals staged" in sent[0][-1]


def test_notify_staged_trims_a_runaway_body(monkeypatch):
    sent = _script_notify_send(monkeypatch)
    config.save({"notify": "always"})
    notify_mod.notify_staged(True, "tab", 1, ["z" * 4000])
    assert len(sent[0][-1]) <= 201 and sent[0][-1].endswith("\u2026")


def test_notify_staged_is_gated_like_any_other_trigger(monkeypatch):
    """A staged proposal is never urgent -- nothing blocks on it and
    nothing reaches curated memory without an explicit approval -- which is
    exactly why it must respect the focus rule instead of interrupting
    someone already looking at DOXA."""
    sent = _script_notify_send(monkeypatch)
    config.save({"notify": "auto"})
    notify_mod.notify_staged(True, "tab", 1, ["x"])  # focused: silent
    assert sent == []
    notify_mod.notify_staged(False, "tab", 1, ["x"])  # unfocused: speaks
    assert len(sent) == 1


def test_notify_staged_toggle_off_silences_it_even_when_unfocused(monkeypatch):
    sent = _script_notify_send(monkeypatch)
    config.save({"notify": "always", "notify_staged": "0"})
    notify_mod.notify_staged(False, "tab", 1, ["x"])
    assert sent == []
