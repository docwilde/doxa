# SPDX-License-Identifier: AGPL-3.0-only
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
* one bool per trigger (``notify_update`` / ``notify_lore`` /
  ``notify_staged``, default ON; ``notify_needs_input``, default OFF --
  see below), so a specific kind of notification can be silenced (or, for
  ``notify_needs_input``, turned on) without touching the others.

A finished turn is deliberately NOT a trigger here. Through v0.84.0 it
was (``notify_turn_done``, fired from every ``turn_done`` regardless of
whether anything was actually waiting on the user), which meant a
perfectly ordinary reply popped a desktop banner -- reported as "response
finished should not trigger a desktop notification. Only when user input
is required." Only a turn genuinely BLOCKED ON THE USER
(:func:`notify_needs_input` -- an ``AskUserQuestion``, a permission
prompt, or a fully detached session with nobody attached at all) is
notification-worthy; a plain completed response is not, and nothing
fires for one. ``notify_needs_input`` itself now defaults OFF (the other
owner request: a way to turn desktop notifications on, which implies
they start off) -- ``_trigger_default`` below reads that straight off
``config.SETTINGS`` (``kind="bool_on"`` -> default on, anything else ->
default off) rather than this module hardcoding a second copy of it.

SINCE v1.7.5 the send itself is also OFF THE EVENT LOOP. Every call site
that reaches :func:`notify` sits on one, ``notify-send`` is a D-Bus client
that can wait, and a blocking ``subprocess.run`` on a Textual loop is a
frozen interface -- no repaint, no keystroke, no timer, and no
``asyncio.wait_for`` anywhere able to expire -- for as long as it waits.
:func:`notify` therefore hands the send to a throwaway daemon thread
whenever a loop is running in the calling thread, and runs it inline when
there is not. No timer, no pool, nothing idle: a thread exists only while a
banner is in flight, which is the right side of v0.78.0's no-always-on-timer
rule.

Focus is tracked by DoxaApp (``events.AppFocus``/``events.AppBlur`` ->
``self.app_has_focus``, init True) and passed in by every call site here --
this module has no window handle of its own. Note the same caveat DoxaApp's
handlers carry: a terminal emulator that never sends focus-reporting
escapes also never sends AppBlur, so ``auto`` degrades to "the window is
always focused" (i.e. never fires) there -- ``always`` is the escape hatch
for exactly that terminal.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import config as config_mod

APP_NAME = "doxa"

NOTIFY_TIMEOUT_SECS = 10.0
"""Ceiling on one ``notify-send``. It is a D-Bus client: with no
notification daemon answering (a session whose daemon died, a
half-configured desktop) it does not fail fast, it waits. Ten seconds is
the ceiling on that wait -- and, before v1.7.5, was also the ceiling on
how long DOXA's event loop could be frozen by a courtesy banner. See
:func:`notify`."""

_inflight: "set[threading.Thread]" = set()
"""Strong references to notification threads still running, so a
fire-and-forget banner cannot be collected mid-flight, and so
:func:`drain_pending` has something to join. Guarded by
``_inflight_lock`` -- entries are added from whatever loop thread
notified and removed from the notifying thread itself."""

_inflight_lock = threading.Lock()


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


def _send(title: str, body: str) -> None:
    """The ``notify-send`` call itself -- BLOCKING, up to
    :data:`NOTIFY_TIMEOUT_SECS`. Never called directly from a coroutine;
    :func:`notify` owns which thread this runs on.

    ``notify-send`` missing (headless, no desktop, an unsupported
    platform) is a silent no-op, and every spawn failure is swallowed the
    same way: this must never be the thing that takes a session down.

    ``except Exception``, not ``except OSError``, and that widening is a
    FIX rather than sloppiness. A ``notify-send`` that hangs raises
    ``subprocess.TimeoutExpired``, which is a ``SubprocessError`` and NOT
    an ``OSError`` -- so through v1.7.4 the one failure this timeout
    exists to contain was the one failure that escaped, straight into
    whatever called it. Two of the four call sites could not survive
    that: ``PaneRuntime._open_needs_input`` would turn a hung banner into
    an error block, and ``SessionDaemon._peer_pump`` would lose its
    ``async for`` and stop fanning events out to every attached client
    for the rest of the process. The docstring above already promised
    this; now it is true."""
    cmd = shutil.which("notify-send")
    if not cmd:
        return
    argv = [cmd, "-a", APP_NAME]
    icon = notify_icon()
    if icon:
        argv += ["-i", icon]
    try:
        subprocess.run(
            argv + [title, body],
            timeout=NOTIFY_TIMEOUT_SECS,
            check=False,
            capture_output=True,
        )
    except Exception:  # noqa: BLE001 -- see the docstring: a courtesy
        # banner may never be the thing that takes a session down, and
        # that includes its own timeout expiring.
        pass


def notify(title: str, body: str) -> None:
    """Desktop notification, unconditionally -- no gating here, that is
    :func:`should_fire`'s job -- and NEVER on the event loop.

    Every one of the four call sites that reach here sits on an event
    loop: ``DoxaApp._check_for_update`` (a worker coroutine -- the
    ``git fetch`` under it is already offloaded with
    ``asyncio.to_thread``, but the notification that follows was not),
    ``PaneRuntime._open_needs_input`` (a message handler),
    ``PaneRuntime._announce_staged`` (a coroutine) and
    ``SessionDaemon._peer_pump`` (the daemon's own fan-out loop). A
    blocking ``subprocess.run`` on any of them freezes everything that
    loop owns for as long as it takes: in a TUI that is no repaint, no
    keystroke handled, no timer fired and no ``asyncio.wait_for``
    anywhere able to expire -- for up to :data:`NOTIFY_TIMEOUT_SECS`.

    So: if a loop is running in this thread, the send goes to a throwaway
    daemon thread and this returns immediately. With no loop running
    (``doxa.daemon`` before it starts serving, a direct call, the tests
    below) there is nothing to protect and the send happens inline, which
    keeps the simple case observable.

    A plain ``threading.Thread``, deliberately, and not
    ``loop.run_in_executor`` / ``asyncio.to_thread``: work handed to the
    default executor is JOINED when the loop shuts down
    (``loop.shutdown_default_executor``), so a hung banner would move the
    freeze from mid-session to exit rather than removing it. A daemon
    thread owes the interpreter nothing on the way out.

    Fire-and-forget, so nothing here reports an error and nothing waits
    -- ordering between two banners racing is not meaningful, they are
    independent desktop toasts. :func:`drain_pending` is how a test
    observes the path anyway."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _send(title, body)  # no loop in this thread; nothing to protect
        return

    thread: "threading.Thread | None" = None

    def _run() -> None:
        try:
            _send(title, body)
        finally:
            with _inflight_lock:
                _inflight.discard(thread)

    thread = threading.Thread(target=_run, name="doxa-notify", daemon=True)
    with _inflight_lock:
        _inflight.add(thread)
    thread.start()


def drain_pending(timeout: float = 5.0) -> None:
    """Join every notification thread still in flight.

    Test-facing and called from nowhere in the product: :func:`notify`
    is fire-and-forget by design, and a caller that waited for it would
    be re-introducing exactly the block this module just removed. A test
    that wants to assert WHAT was sent (or which thread sent it) needs a
    deterministic point after the send, and this is it -- an assertion
    that instead slept for a moment would be a timing assertion, and this
    codebase has enough of those to know how they end."""
    with _inflight_lock:
        threads = list(_inflight)
    for thread in threads:
        thread.join(timeout)


def _mode() -> str:
    """Effective master mode -- auto, always or off. An unrecognised value
    (a hand-edited config, a typo'd env var) degrades to ``auto`` rather
    than either silencing everything or spamming everything."""
    value = config_mod.raw("DOXA_NOTIFY").strip().lower()
    return value if value in ("auto", "always", "off") else "auto"


def _trigger_default(trigger_env: str) -> bool:
    """Whether a trigger notifies out of the box, when nothing has ever
    set its env var or its config-file key -- read straight off the
    settings registry rather than redeclared here, so there is exactly
    ONE place a trigger's default lives. ``kind="bool_on"`` (config.py's
    own declaration for a bool that defaults ON, e.g. ``notify_staged``)
    means yes; anything else -- in practice ``kind="bool"``, the
    registry's declaration for a bool that defaults OFF, which is what
    ``notify_needs_input`` uses since v0.85.0 -- means no. A trigger with
    no matching Setting at all (should not happen; every trigger here has
    one) also defaults off, the safer failure."""
    setting = config_mod.SETTINGS_BY_ENV.get(trigger_env)
    return bool(setting is not None and setting.kind == "bool_on")


def should_fire(trigger_env: str, app_has_focus: bool) -> bool:
    """Whether ONE trigger should actually notify right now: its own
    per-trigger bool AND the master mode AND (for ``auto``) focus."""
    if not _bool(trigger_env, _trigger_default(trigger_env)):
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


def notify_needs_input(app_has_focus: bool, tab_label: str, summary: str) -> None:
    """A session is waiting on you -- queue item 5's ``can_use_tool``
    plumbing: an ``AskUserQuestion`` question, or a tool call the CLI
    would have shown its own permission prompt on. ``summary`` is already
    scrubbed by the caller (``doxa.engine``'s secret-scrub choke point,
    or ``doxa.daemon``'s own detached-session call site) before it ever
    reaches here -- this function trusts it, same as ``notify`` trusts
    every other body it is handed. Truncated: a desktop banner is not the
    place for a full question or tool-call payload."""
    body = (summary or "").strip() or "needs your input"
    if len(body) > 120:
        body = body[:120] + "…"
    notify_if("DOXA_NOTIFY_NEEDS_INPUT", app_has_focus, tab_label, body)


def notify_staged(
    app_has_focus: bool,
    tab_label: str,
    staged: int,
    texts: "list[str] | None" = None,
) -> None:
    """The background reviewer staged something -- the streaming deriver
    (``DOXA_DERIVE_SECS``) extracted material from the live transcript and
    did NOT reject it, so it is now sitting behind LORE's approval gate
    waiting on a human.

    Title is the tab's own label, same as :func:`notify_needs_input`: in a
    multi-tab window "which session derived this" is the first question.
    The body leads with the count and then QUOTES the first proposal,
    because a bare count cannot tell you whether a batch is worth opening.
    ``texts`` arrive already scrubbed and ellipsized from the producer
    (``doxa.engine.staged_event_payload``) -- trusted here, the same way
    :func:`notify_needs_input` trusts its summary -- and trimmed once more
    only to keep a desktop banner banner-sized.

    Gated like every other trigger: ``DOXA_NOTIFY_STAGED`` AND the master
    mode AND (on ``auto``) focus. Nothing about a staged proposal is
    urgent -- it waits indefinitely, and nothing reaches curated memory
    without an explicit approval -- which is precisely why it must obey
    the focus rule rather than interrupt someone already looking."""
    noun = "proposal" if staged == 1 else "proposals"
    body = f"{staged} {noun} staged by the background reviewer"
    first = next((t for t in (texts or []) if t.strip()), "")
    if first:
        body += f"\n{first.strip()}"
    if len(body) > 200:
        body = body[:200] + "…"
    notify_if("DOXA_NOTIFY_STAGED", app_has_focus, tab_label, body)


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

    ONE MORE INPUT SINCE v0.31.0, and it is the fix for what the previous
    revision of this docstring recorded as an open gap ("lore_core's own
    notification has no DOXA focus awareness ... so with notify_lore=on a
    staged-proposal notification fires even while the DOXA window is
    focused. Closing that gap needs doxa/engine.py to call through
    doxa.notify itself"). DOXA now DOES: :func:`notify_staged` fires from
    the TUI's own ``derive_done`` handler, focus-gated like every other
    trigger and carrying the proposal text lore_core's banner never had.
    Two notifiers for one event would mean two banners, so whichever of
    them is better informed wins:

    notify_staged on -> ``LORE_NOTIFY=0`` regardless of notify_lore. DOXA
                         owns this banner now; lore_core's blunter one
                         would only duplicate it, unfocused-unaware.
    notify_staged off-> the pre-v0.31.0 rule above, unchanged: notify_lore
                         alone decides whether lore_core speaks. Turning
                         DOXA's own trigger off therefore does not leave
                         the user with silence they did not ask for.

    Both branches route through the same ``_lore_notify_silenced_by_us``
    latch, so a user's OWN ``LORE_NOTIFY`` choice made in their shell is
    still never clobbered on the way back.
    """
    global _lore_notify_silenced_by_us
    silence = _bool("DOXA_NOTIFY_STAGED", True) or not _bool("DOXA_NOTIFY_LORE", True)
    if silence:
        os.environ["LORE_NOTIFY"] = "0"
        _lore_notify_silenced_by_us = True
    elif _lore_notify_silenced_by_us:
        os.environ.pop("LORE_NOTIFY", None)
        _lore_notify_silenced_by_us = False
