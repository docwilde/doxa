# SPDX-License-Identifier: AGPL-3.0-only
"""The keys splits and dividers claim (v0.91.0), and the ones they must
not.

This file exists because a binding collision is invisible until a user
hits it: two bindings on one key resolve to whichever the merge kept, and
the losing feature simply appears not to work. The spec picked Ctrl+Up /
Ctrl+Down for the divider on a set that was verified free on 2026-08-25 --
and the set CHANGED afterwards (v0.85.0 freed Ctrl+C for terminal copy and
popped Textual's own binding for it). So the check is re-run here, against
the binding table as it actually resolves, rather than trusted from the
document.
"""

from __future__ import annotations

from doxa.app import DoxaApp


def _resolved(app: DoxaApp) -> dict:
    """Every key DOXA answers, as Textual's own merge resolved it -- App
    defaults, Screen defaults and DoxaApp.BINDINGS together, including
    the instance-level Ctrl+C removal __init__ performs."""
    return dict(app._bindings.key_to_bindings)


def _action_for(app: DoxaApp, key: str) -> "list[str]":
    return [b.action for b in _resolved(app).get(key, [])]


def test_the_divider_keys_are_claimed_by_the_divider_and_nothing_else():
    app = DoxaApp(cwd=".")
    assert _action_for(app, "ctrl+up") == ["divider_up"]
    assert _action_for(app, "ctrl+down") == ["divider_down"]


def test_the_divider_keys_survive_a_focused_prompt():
    """``PromptInput`` is a ``TextArea`` and holds focus nearly always;
    TextArea binds ctrl+up/ctrl+down to cursor movement of its own.
    Without priority the widget eats the key before the app sees it,
    which is the same reason ctrl+t, ctrl+comma and ctrl+left/right are
    all declared this way."""
    app = DoxaApp(cwd=".")
    for key in ("ctrl+up", "ctrl+down"):
        bindings = _resolved(app)[key]
        assert bindings and all(b.priority for b in bindings), key


def test_the_split_and_pane_keys_do_not_collide_with_the_existing_set():
    """The pre-v0.91.0 set, written out rather than derived, so a future
    release that rebinds one of them trips over this list instead of
    silently taking a key splits already answer."""
    established = {
        "ctrl+p", "ctrl+r", "ctrl+comma", "ctrl+t", "ctrl+w", "ctrl+q",
        "ctrl+left", "ctrl+right", "shift+tab", "ctrl+tab",
    }
    claimed = {
        "ctrl+up", "ctrl+down",
        "ctrl+shift+left", "ctrl+shift+right",
        "ctrl+shift+up", "ctrl+shift+down",
        "alt+s", "alt+d",
        "alt+up", "alt+down", "alt+left", "alt+right",
    }
    assert established & claimed == set()

    app = DoxaApp(cwd=".")
    resolved = _resolved(app)
    for key in claimed:
        assert key in resolved, f"{key} is documented but not bound"
        assert len(resolved[key]) == 1, f"{key} resolves to more than one action"


def test_the_split_keys_follow_vim_not_tmux():
    """The two conventions mean opposite things by the same words, so the
    letters have to agree with the command names or nothing does.

    vim: `:split` is STACKED, `:vsplit` is SIDE BY SIDE. tmux:
    `split-window -h` gives SIDE-BY-SIDE panes, because it splits along
    the horizontal axis. DOXA's commands were already named with vim's
    meanings, so H rides with `/split` and V with `/vsplit` -- and both
    the binding description and the registry summary spell the direction
    out in words, because the letter alone cannot resolve the ambiguity
    for someone who knows the other convention."""
    from doxa import commands as commands_mod

    app = DoxaApp(cwd=".")
    assert _action_for(app, "alt+s") == ["split_pane"]
    assert _action_for(app, "alt+d") == ["vsplit_pane"]

    stacked = _resolved(app)["alt+s"][0].description
    beside = _resolved(app)["alt+d"][0].description
    assert "STACKED BELOW" in stacked and "/split" in stacked
    assert "SIDE BY SIDE" in beside and "/vsplit" in beside

    assert "STACKED BELOW" in commands_mod.lookup("/split").summary
    assert "SIDE BY SIDE" in commands_mod.lookup("/vsplit").summary


def test_ctrl_c_is_still_unbound():
    """v0.85.0 freed it for the terminal emulator's own copy gesture and
    popped Textual's own default. Nothing this release adds may take it
    back -- and the split keys added here deliberately avoid the whole
    class of contested chords: Alt+S / Alt+D are an ESC prefix no
    terminal claims, unlike the ctrl+shift+v this release first drafted,
    which is most emulators' own paste gesture."""
    assert "ctrl+c" not in _resolved(DoxaApp(cwd="."))


def test_split_and_vsplit_are_real_commands_with_help_and_palette_rows():
    """Every action has a command and, where it earns one, a binding.
    Alt+S / Alt+D reach every terminal, but the command is still the
    door that never depends on an encoding at all."""
    from doxa import commands as commands_mod
    from doxa.session.commands import PANE_COMMANDS

    names = {c.name for c in commands_mod.REGISTRY}
    assert {"/split", "/vsplit"} <= names

    handlers = {c.name: c.method for c in PANE_COMMANDS}
    assert handlers["/split"] == "_cmd_split"
    assert handlers["/vsplit"] == "_cmd_vsplit"

    for name in ("/split", "/vsplit"):
        row = commands_mod.lookup(name)
        assert row is not None and not row.passthrough
        assert row.group == "Panes & tabs"
