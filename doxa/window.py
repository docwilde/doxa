# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.window -- the title on the terminal's own window and taskbar entry.

Distinct from :attr:`textual.app.App.title`, which is DOXA's business
INSIDE its own frame (the `Header` widget's caption) and never leaves the
process. What a window manager puts on the taskbar button, and what a tab
bar in a terminal emulator prints, is set by an escape sequence on the
output stream -- and Textual 5.3.0 has no API for it. Measured, not
assumed: the only OSC the installed Textual writes is `OSC 52` for the
clipboard (`textual/app.py`, `_set_clipboard`); there is no
`set_terminal_title`, no driver hook, and `App.TITLE`/`App.title` are a
plain reactive consumed by `Header`. So DOXA writes the sequence itself,
which is what this module is.

**Setting it is the easy half. Giving it back is the point.** A terminal
title is process-global state with no owner and no query: OSC 21 (report
the title) is disabled by default in every terminal that ever shipped it,
because a program that can read the title can read whatever the previous
program left there. So there is nothing to save and restore by hand, and
an app that just sets a title leaves the user's window called "DOXA"
until they notice and fix it themselves -- which they cannot, because
they do not know the escape either.

The answer is the title STACK, xterm's `CSI 22 t` / `CSI 23 t`, which
every terminal that matters implements (xterm, kitty, alacritty,
foot, wezterm, gnome-terminal/vte, konsole, tmux, screen):

* `CSI 22 ; 0 t` pushes the current window AND icon titles onto a stack
  held by the TERMINAL -- the one process that does know what they are.
* `CSI 23 ; 0 t` pops them back off.

:func:`terminal_title` is a context manager around that pair, and the pop
is in a ``finally``, which is what makes it hold on the paths that matter:

* **Normal exit** -- ``App.run()`` returns, the ``with`` block ends.
* **Ctrl+C** -- two cases, both covered. DOXA binds ``ctrl+c`` itself
  (``DoxaApp.action_ctrl_c_quit``, ``priority=True``) so the ordinary
  press is a quit action and returns normally; and if the key arrives
  before the app is live, ``KeyboardInterrupt`` propagates out of
  ``run()`` and through the ``finally``.
* **A crash** -- any exception out of ``run()`` unwinds through the
  ``finally`` before the traceback prints.

What it does NOT survive is ``SIGKILL`` and a hard ``SIGTERM``, and this
module deliberately does not install signal handlers to chase them:
Textual installs its own, a second one racing it is how a shutdown path
acquires a Heisenbug, and a terminal that lost its title to `kill -9`
gets it back from the next shell prompt anyway (bash's `PROMPT_COMMAND`
and zsh's `precmd` both repaint it).

A terminal WITHOUT the stack ignores both sequences, keeps the title DOXA
set, and repaints on the next prompt for the same reason. That is the
floor, and it is the same floor `vim`, `htop` and `tmux` accept for the
same reason -- there is no better one available.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import IO, Iterator

#: OSC 0 sets the icon name AND the window title in one sequence. OSC 2
#: would set only the window title, which is the narrower and more
#: "correct" answer -- and the wrong one here: the icon name is what a
#: number of window managers actually put on the taskbar button, which is
#: half of what was asked for. Terminated with BEL rather than ST because
#: BEL is what every terminal accepts and some older ones require.
_SET = "\x1b]0;{title}\x07"

#: Push/pop BOTH titles (the ``0`` selects icon + window), so the pop
#: undoes exactly what the OSC 0 above overwrote.
_PUSH = "\x1b[22;0t"
_POP = "\x1b[23;0t"

#: Opt-out. Not a config setting: it exists for the person whose terminal
#: multiplexer, prompt or window manager already owns the title and who
#: wants DOXA to keep its hands off -- which is a property of their
#: environment, not of their DOXA, and belongs where the environment is.
OPT_OUT_ENV = "DOXA_NO_TERMINAL_TITLE"

#: Titles are a single line by construction: a newline in one would end
#: the escape sequence early and dump the rest onto the screen as text.
#: Anything that could carry a control character is scrubbed rather than
#: trusted, because the repo name below comes from a directory the user
#: did not necessarily create.
_MAX_TITLE_LEN = 96


def title_for(cwd: "str | Path | None" = None) -> str:
    """The window title: ``DOXA`` alone, or ``DOXA — <project>``.

    The user asked for "DOXA", and ``DOXA`` alone is what a run outside
    any project gets. The project name is added because of WHERE this
    string lands: a taskbar button and a terminal tab exist to tell two
    windows apart, and the thing that distinguishes two DOXA windows is
    never the application -- it is always the repository. Three windows
    all called "DOXA" is a taskbar that has stopped working as a taskbar.

    What it deliberately does NOT carry is the active session. The title
    would then change on every tab switch, and a taskbar button whose
    label moves under the pointer is worse than a stale one; the session
    is already named on the tab strip, which is inside the window where a
    user is actually looking at it. The window title answers "which
    window", once."""
    name = ""
    if cwd:
        with contextlib.suppress(OSError, ValueError):
            resolved = Path(cwd).resolve()
            # Not the git root: DOXA opens sessions in subdirectories on
            # purpose (doxa/worktrees.py), and "which directory am I in"
            # is the more useful of the two answers on a taskbar.
            name = resolved.name if resolved.parent != resolved else ""
    if not name:
        return "DOXA"
    return f"DOXA — {_sanitise(name)}"


def _sanitise(text: str) -> str:
    """A title with no control characters and a bounded length.

    A newline or a BEL inside the payload would terminate the escape and
    spray the remainder across the user's screen; a directory name is
    attacker-influenced input on a machine that clones repositories, so
    it is filtered rather than trusted."""
    cleaned = "".join(char for char in text if char.isprintable())
    if len(cleaned) > _MAX_TITLE_LEN:
        cleaned = cleaned[: _MAX_TITLE_LEN - 1] + "…"
    return cleaned


def supported(stream: "IO[str] | None" = None) -> bool:
    """Should this process touch the title at all?

    Three refusals, each for a different reason:

    * **Not a terminal.** Output is a pipe or a file; an escape sequence
      there is corruption of somebody's data, not a title.
    * **``TERM`` is absent or ``dumb``.** The documented way to say "this
      thing does not do escapes".
    * **:data:`OPT_OUT_ENV` is set.** The user said no.

    A stream that raises on ``isatty`` (a closed or exotic file object)
    counts as not a terminal -- refusing is always the safe direction
    here, because the cost of a wrong "yes" is garbage on the screen and
    the cost of a wrong "no" is a title that stays as it was."""
    if os.environ.get(OPT_OUT_ENV, "").strip():
        return False
    if os.environ.get("TERM", "").strip().lower() in ("", "dumb"):
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 -- a stream that cannot answer is a no
        return False


def _emit(text: str, stream: "IO[str] | None") -> None:
    """Write one sequence and flush it.

    Flushed immediately and unconditionally: these go out either side of
    a full-screen TUI that owns the stream in between, and a title sitting
    in a buffer until the app happens to write something else is a title
    that appears at the wrong moment -- or, at exit, never."""
    stream = stream if stream is not None else sys.stdout
    with contextlib.suppress(Exception):
        stream.write(text)
        stream.flush()


def set_title(title: str, stream: "IO[str] | None" = None) -> None:
    """Set the window + icon title. No-op where :func:`supported` says so."""
    if not supported(stream):
        return
    _emit(_SET.format(title=_sanitise(title)), stream)


def push_title(stream: "IO[str] | None" = None) -> None:
    """Ask the TERMINAL to remember the title it currently shows."""
    if not supported(stream):
        return
    _emit(_PUSH, stream)


def pop_title(stream: "IO[str] | None" = None) -> None:
    """Give the remembered title back."""
    if not supported(stream):
        return
    _emit(_POP, stream)


@contextlib.contextmanager
def terminal_title(
    title: str, stream: "IO[str] | None" = None
) -> "Iterator[None]":
    """Own the terminal title for the duration of the block, and hand it
    back afterwards -- including when the block raises.

    Push BEFORE set, pop AFTER: the stack has to capture the user's title
    while it is still on screen. The pop is the entire reason this is a
    context manager rather than two calls, so it is in a ``finally`` and
    the caller cannot forget it."""
    push_title(stream)
    try:
        set_title(title, stream)
        yield
    finally:
        pop_title(stream)
