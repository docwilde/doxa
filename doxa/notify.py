"""doxa.notify -- desktop notifications: notify-send, focus-gated, per-trigger toggles.

Same shape as LORE's own notifier (``lore_core.deriver.notify_icon``/
``notify``, ~line 929 of that module): a plain ``notify-send`` subprocess,
an icon-theme name or path from an env var, and a silent no-op when
``notify-send`` is not on PATH. Nothing here ever raises -- a notification
is a courtesy, and a courtesy that can crash the terminal is not one.

What DOXA adds on top of that shape is the GATING, because a TUI's own
window can be focused or not (LORE's caller is a background daemon with no
such concept):

* a master switch, ``notify`` (env ``DOXA_NOTIFY``): ``auto`` (only when
  the terminal window does NOT have focus -- a notification that says
  "look at the terminal" is noise if you are already looking at it),
  ``always`` (ignore focus), or ``off`` (never).
* one bool per trigger (``notify_turn_done`` / ``notify_update`` /
  ``notify_lore`` / ``notify_needs_input``, all default ON), so a specific
  kind of notification can be silenced without killing the others.

Focus is tracked by DoxaApp (``events.AppFocus``/``events.AppBlur`` ->
``self.app_has_focus``, init True) and passed in by every call site here --
this module has no window handle of its own. Note the same caveat DoxaApp's
handlers carry: a terminal emulator that never sends focus-reporting
escapes also never sends AppBlur, so ``auto`` degrades to "the window is
always focused" (i.e. never fires) there -- ``always`` is the escape hatch
for exactly that terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import config as config_mod

APP_NAME = "doxa"


def _bool(env_name: str, default: bool) -> bool:
    """Same truthy/falsy vocabulary as ``config._coerce``'s ``bool`` kind,
    but able to default ON -- ``config.raw`` returns "" for both "never
    set" and "explicitly off", and only the caller knows which default that
    silence should mean. Identical helper to ``doxa.clock._bool``; kept
    local rather than imported so this module has no dependency on the
    clock's, both being small enough that the duplication costs nothing."""
    raw = config_mod.raw(env_name).strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


def notify_icon() -> "str | None":
    """What to draw on the notification, or None to let notify-send /
    the desktop's own default decide. ``DOXA_NOTIFY_ICON`` accepts either
    an icon-theme name (passed through untouched) or a path (only once it
    exists -- notify-send given a missing file renders a blank space
    rather than falling back), the same two-shape contract LORE's own
    ``notify_icon`` documents. Unlike LORE, DOXA ships no bundled mark
    (yet), so there is no shipped-asset fallback: unset means no ``-i``
    flag at all."""
    override = config_mod.raw("DOXA_NOTIFY_ICON").strip()
    if not override:
        return None
    return override if "/" not in override or Path(override).is_file() else None


def notify(title: str, body: str) -> None:
    """Desktop notification, unconditionally -- no gating here, that is
    :func:`should_fire`'s job. ``notify-send`` missing (headless, no
    desktop, an unsupported platform) is a silent no-op, and any spawn
    failure is swallowed the same way: this must never be the thing that
    takes a session down."""
    cmd = shutil.which("notify-send")
    if not cmd:
        return
    argv = [cmd, "-a", APP_NAME]
    icon = notify_icon()
    if icon:
        argv += ["-i", icon]
    try:
        subprocess.run(
            argv + [title, body], timeout=10, check=False, capture_output=True
        )
    except OSError:
        pass


def _mode() -> str:
    """Effective master mode -- auto, always or off. An unrecognised value
    (a hand-edited config, a typo'd env var) degrades to ``auto`` rather
    than either silencing everything or spamming everything."""
    value = config_mod.raw("DOXA_NOTIFY").strip().lower()
    return value if value in ("auto", "always", "off") else "auto"


def should_fire(trigger_env: str, app_has_focus: bool) -> bool:
    """Whether ONE trigger should actually notify right now: its own
    per-trigger bool AND the master mode AND (for ``auto``) focus."""
    if not _bool(trigger_env, True):
        return False
    mode = _mode()
    if mode == "off":
        return False
    if mode == "always":
        return True
    return not app_has_focus  # auto: only while unfocused


def notify_if(trigger_env: str, app_has_focus: bool, title: str, body: str) -> None:
    """Gate then send -- the one call site every trigger below funnels
    through, so the gating logic lives in exactly one place."""
    if should_fire(trigger_env, app_has_focus):
        notify(title, body)


# -- the triggers wired now -------------------------------------------------


def notify_turn_done(
    app_has_focus: bool, tab_label: str, duration_ms: "float | None"
) -> None:
    """A turn finished. Title is the tab's own label (which pane, in a
    multi-tab window), body is short -- a duration when the caller has one,
    since "how long did that take" is the one thing worth a glance."""
    body = "response finished"
    if duration_ms:
        body += f" ({duration_ms / 1000:.1f}s)"
    notify_if("DOXA_NOTIFY_TURN_DONE", app_has_focus, tab_label, body)


def notify_update_available(app_has_focus: bool) -> None:
    """A fast-forward is sitting on the remote, unpulled. Fires at most
    once per app run (the caller owns that -- this function is stateless)."""
    notify_if(
        "DOXA_NOTIFY_UPDATE", app_has_focus,
        "DOXA update available", "/update",
    )


# -- LORE inheritance ---------------------------------------------------


_lore_notify_silenced_by_us = False
"""Tracks whether THIS process is the one holding LORE_NOTIFY=0, so turning
notify_lore back on restores whatever was there before (nothing, in the
common case) rather than clobbering a value the user set in their own
shell."""


def sync_lore_notify_env() -> None:
    """Make lore_core's own in-process notification (``deriver.notify_staged``,
    fired synchronously off ``doxa.engine``'s review path -- see
    ``SessionEngine._run_review_sync`` -> ``lore_deriver.worker_run`` ->
    ``notify_staged`` -> ``notify``) agree with DOXA's ``notify_lore``
    toggle, without editing ``doxa/engine.py`` (out of this feature's scope)
    or ``lore_core`` (read-only import, per ``_lore_bootstrap``'s
    docstring).

    This works because ``lore_core.deriver.notify()`` reads ``LORE_NOTIFY``
    fresh on EVERY call rather than caching it at import time (unlike
    ``LORE_ROOT``, which is read once at ``lore_core`` import and is why
    ``_lore_bootstrap.export_sticky_lore_root`` has to run before that
    import) -- so setting the env var any time before a notification would
    fire is early enough, and this can be called repeatedly (app start, and
    again whenever the settings modal saves) with no ordering constraint.

    notify_lore off  -> ``LORE_NOTIFY=0``, silencing lore_core's notify()
                         outright (it has no separate per-title toggle to
                         aim at).
    notify_lore on   -> leaves the var alone, UNLESS this same process was
                         the one that set it to "0" a moment ago, in which
                         case that override is undone.

    What this does NOT achieve -- documented rather than silently
    pretended away: lore_core's own notification has no DOXA focus
    awareness (LORE_NOTIFY is a blunt on/off, not an auto/always/off mode
    like DOXA's own ``notify`` setting), so with notify_lore=on a staged-
    proposal notification fires even while the DOXA window is focused.
    Closing that gap needs ``doxa/engine.py`` to call through
    ``doxa.notify`` itself instead of relying on lore_core's own notify()
    -- out of scope here; see the task report.
    """
    global _lore_notify_silenced_by_us
    if _bool("DOXA_NOTIFY_LORE", True):
        if _lore_notify_silenced_by_us:
            os.environ.pop("LORE_NOTIFY", None)
            _lore_notify_silenced_by_us = False
    else:
        os.environ["LORE_NOTIFY"] = "0"
        _lore_notify_silenced_by_us = True
