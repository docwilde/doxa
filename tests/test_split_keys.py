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
        "ctrl+o", "ctrl+n",
        # The kitty-only aliases the primaries replaced in v0.95.0. Still
        # bound, so still listed: this set's job is to make the NEXT
        # release trip over a collision rather than ship one, and a key
        # left out because it is "only" an alias is a key the next
        # feature quietly takes.
        "alt+s", "alt+d",
        "alt+up", "alt+down", "alt+left", "alt+right",
        # v0.92.0's live diff. Added HERE and not only in
        # tests/test_live_diff.py because this is the list whose whole
        # job is to make the NEXT release trip over a collision instead
        # of shipping one -- a key checked only where its own feature is
        # tested is a key the next feature can quietly take.
        "f2", "alt+g",
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
    assert _action_for(app, "ctrl+o") == ["split_pane"]
    assert _action_for(app, "ctrl+n") == ["vsplit_pane"]
    # The kitty-tier aliases reach the SAME actions -- a second key that
    # did something subtly different would be worse than no second key.
    assert _action_for(app, "alt+s") == ["split_pane"]
    assert _action_for(app, "alt+d") == ["vsplit_pane"]

    stacked = _resolved(app)["ctrl+o"][0].description
    beside = _resolved(app)["ctrl+n"][0].description
    assert "STACKED BELOW" in stacked and "/split" in stacked
    assert "SIDE BY SIDE" in beside and "/vsplit" in beside

    assert "STACKED BELOW" in commands_mod.lookup("/split").summary
    assert "SIDE BY SIDE" in commands_mod.lookup("/vsplit").summary


def test_ctrl_c_is_still_unbound():
    """v0.85.0 freed it for the terminal emulator's own copy gesture and
    popped Textual's own default. Nothing this release adds may take it
    back -- and the split keys added here deliberately avoid the whole
    class of contested chords -- and v0.95.0 widened that avoidance to
    the widget layer, which is where the replacement keys had to be
    chosen: Textual's own TextArea (and the prompt IS one) binds
    ctrl+a/c/d/e/f/k/u/v/w/x/y/z, so a priority binding on any of them
    would win the key and break line editing with it."""
    assert "ctrl+c" not in _resolved(DoxaApp(cwd="."))


def test_split_and_vsplit_are_real_commands_with_help_and_palette_rows():
    """Every action has a command and, where it earns one, a binding.
    The command is the door that never depends on a key encoding at all,
    which is exactly what kept the feature usable through v0.91.0-0.94.0
    while its advertised hotkeys reached only kitty-protocol terminals:
    the live report was "the hotkeys Alt+D and Alt+S are unresponsive",
    not "splitting is broken"."""
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


# -- the encoding, not the binding table (v0.95.0) ---------------------
#
# Everything above resolves DoxaApp's binding table, and `pilot.press`
# feeds Textual a key NAME. Neither touches the layer where v0.91.0's
# split keys actually failed: the bytes a terminal sends and what
# Textual's parser makes of them. Three releases of tests passed over a
# hotkey that could not arrive, because no test ever asked.


def _parsed(data: str) -> "list[str]":
    """The key names textual's own parser produces for `data`, exactly as
    the Linux driver feeds it -- no app, no pilot, no binding table."""
    from textual import events
    from textual._xterm_parser import XTermParser

    parser = XTermParser()
    return [
        event.key
        for event in [*parser.feed(data), *parser.feed("")]
        if isinstance(event, events.Key)
    ]


def test_textual_cannot_decode_alt_from_an_esc_prefix():
    """The measurement that condemned Alt+S / Alt+D / Alt+G.

    A terminal without the kitty protocol sends Alt+X as ESC then X, and
    has since long before the protocol existed -- which is what v0.91.0
    reasoned from, and it is true. What it did not check is that textual
    5.3.0 has no ESC-prefix-to-Alt path: the string "alt" occurs once in
    ``textual/_xterm_parser.py``, inside the CSI-u modifier table, and
    ``_ansi_sequences.py`` hand-maps a few two-byte ESC pairs to
    ctrl+arrow and ctrl+w and no letter to Alt.

    So the app is handed a bare Escape and then the naked character, the
    binding never fires, and a focused prompt types the letter."""
    assert _parsed("\x1bs") == ["escape", "s"]
    assert _parsed("\x1bd") == ["escape", "d"]
    assert _parsed("\x1bg") == ["escape", "g"]
    # ...and the kitty encoding of the same key, which is the ONLY way
    # those bindings ever fired. 115/100/103 are s/d/g; ;3 is Alt.
    assert _parsed("\x1b[115;3u") == ["alt+s"]
    assert _parsed("\x1b[100;3u") == ["alt+d"]
    assert _parsed("\x1b[103;3u") == ["alt+g"]


def test_the_keys_the_splits_now_use_decode_under_the_legacy_encoding():
    """Every primary the split family advertises, as its legacy bytes.

    Ctrl+O and Ctrl+N are C0 codes (0x0f, 0x0e), which is the encoding
    ctrl+<letter> was built around; F2 arrives as SS3 Q from an
    application-mode terminal and as CSI 12~ from the others, and both
    are older than the problem this test is about."""
    assert _parsed("\x0f") == ["ctrl+o"]
    assert _parsed("\x0e") == ["ctrl+n"]
    assert _parsed("\x1bOQ") == ["f2"]
    assert _parsed("\x1b[12~") == ["f2"]
    # The divider keys KEPT their Alt, and this is why: a modified ARROW
    # is CSI 1;<mods><final>, not an ESC prefix.
    assert _parsed("\x1b[1;3D") == ["alt+left"]
    assert _parsed("\x1b[1;3C") == ["alt+right"]
    assert _parsed("\x1b[1;3A") == ["alt+up"]
    assert _parsed("\x1b[1;3B") == ["alt+down"]


def test_no_primary_binding_is_unreachable_under_the_legacy_encoding():
    """The general form of the defect, so the next release cannot repeat
    it in a new key.

    Every action DoxaApp binds should have at least ONE key a legacy
    terminal can deliver. A kitty-only key may ride BESIDE one -- Ctrl+Tab
    has ridden beside Shift+Tab since v0.42.0 -- but an action reachable
    ONLY that way is a feature most users cannot press, which is exactly
    what /split, /vsplit and /diff were from v0.91.0 to v0.94.0.

    ``settings`` is the one sanctioned exception and is named here rather
    than filtered out silently. Ctrl+, has no legacy byte at all (there
    is no C0 code for Ctrl+comma), so no second key would help; its door
    is the ``/settings`` command, /help marks the key ✗ where it cannot
    arrive, and /doctor lists it. That is the v0.39.0 arrangement working
    as designed -- the defect this test exists for is the one where a key
    is documented as working and is not."""
    from doxa import keyboard as keyboard_mod

    keys_by_action: dict[str, list[str]] = {}
    for binding in DoxaApp.BINDINGS:
        keys_by_action.setdefault(binding.action, []).append(binding.key)

    orphaned = {
        action: keys
        for action, keys in keys_by_action.items()
        if all(keyboard_mod.unreachable_under_legacy(key) for key in keys)
    }
    assert orphaned.keys() == {"settings"}, (
        "these actions are reachable only on a kitty-protocol terminal: "
        f"{orphaned}"
    )
    for action in ("split_pane", "vsplit_pane", "toggle_diff"):
        assert action in keys_by_action
        assert any(
            not keyboard_mod.unreachable_under_legacy(key)
            for key in keys_by_action[action]
        ), f"{action} is back to being kitty-only"


def test_the_split_keys_do_not_contest_what_the_prompt_owns():
    """The prompt is a ``TextArea`` and the split bindings are
    ``priority=True``, so any key TextArea binds would be TAKEN from it,
    not shared with it -- line editing would break in exchange for a
    split. That constraint is what narrowed ctrl+<letter> to exactly two
    candidates, so it is asserted rather than left in a comment."""
    from textual.widgets import TextArea

    from doxa.ui.prompt import PromptInput

    text_area_keys = {
        key.strip()
        for binding in TextArea.BINDINGS
        for key in binding.key.split(",")
    }
    # PromptInput inherits all of them and only no-ops ctrl+v.
    assert "ctrl+v" in text_area_keys
    assert {b.key for b in PromptInput.BINDINGS} == {"ctrl+v"}

    for key in ("ctrl+o", "ctrl+n", "f2"):
        assert key not in text_area_keys, f"{key} is TextArea's, not ours"
