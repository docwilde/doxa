# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.keyboard -- which keyboard protocol this terminal actually grants,
and which bound keys it therefore cannot deliver.

**Why this module exists.** Under the legacy (VT/xterm) key encoding a
terminal has no way to say *Ctrl+,* or *Shift+Enter* at all: control keys
are the 33 C0 codes and nothing else, so ``Ctrl+,`` produces no byte and
``Shift+Enter`` produces the same ``\\r`` that bare Enter does. The kitty
keyboard protocol fixes that by reporting keys as ``CSI <codepoint>;<mods>u``.
DOXA binds ``ctrl+comma`` to /settings and reads ``shift+enter`` at the
prompt; on a terminal without the protocol both are silently dead, and
nothing anywhere told the user whether DOXA or the terminal was at fault.

**What Textual reports: nothing.** Textual 5.3.0's Linux driver *requests*
the protocol unconditionally on startup --
``textual/drivers/linux_driver.py:276`` writes ``\\x1b[>1u`` -- and disables
it again at ``:373``. It never asks whether the request was granted and
keeps no flag for it. Contrast the in-band window resize right beside it,
which IS queried (``_query_in_band_window_resize``, ``:149``), answered
through ``messages.InBandWindowResize`` and remembered on the driver
(``_in_band_window_resize``, ``:64``) *and* on the app
(``App.supports_smooth_scrolling``, ``textual/app.py:822``). There is no
equivalent for the keyboard: no ``App`` attribute, no ``Driver`` property,
no message. ``textual/_xterm_parser.py:326`` will happily decode a ``CSI u``
key if one arrives, but that is a parse, not a report -- it can only tell us
anything after the user has already pressed a key we may not be able to
receive.

**Alt is not the escape hatch it looks like.** v0.91.0 moved the split
keys onto Alt+letter believing Alt reached every terminal because every
terminal has sent it as an ESC prefix forever. Measured in v0.95.0, that
is true of the terminal and false of Textual: its parser has no
ESC-prefix-to-Alt path, so ``alt+s`` only ever arrives on a terminal that
granted the kitty protocol. :data:`_ALT_ONLY_UNDER_KITTY` carries the
transcript.

So DOXA asks the terminal itself, using the protocol's own support query:

    ``\\x1b[?u``   -- "which progressive-enhancement flags are set?"
    ``\\x1b[c``    -- Primary Device Attributes, the sentinel

A terminal that implements the protocol answers ``\\x1b[?<flags>u`` and then
its DA report; one that does not answers only the DA report. The DA sentinel
is what makes this honest rather than a guess: **silence is never read as
"legacy"**. If nothing comes back at all -- stdin was not ours to read,
the terminal is slow, we are inside a running Textual app whose reader
thread ate the reply -- the answer is :data:`UNKNOWN`, and every surface
says so rather than inventing a capability claim in either direction.

**Detection discipline** is :mod:`doxa.images`', for the same reason and
with the same failure mode: the probe writes escapes and reads the reply
from stdin, which Textual's reader thread grabs the moment ``App.run()``
starts. So it runs AT MOST ONCE, cached module-wide, and ``DoxaApp.__init__``
settles it (beside the image probe) while this process still owns the
terminal. Headless -- pytest, a pipe, ``doxa doctor`` in CI -- ``_is_tty()``
is False and the probe short-circuits to "unknown" without writing a byte.

``DOXA_KEYBOARD_PROTOCOL`` (kitty | legacy | unknown) overrides detection
entirely, checked per call, so tests exercise each tier without touching
the cache and a user whose terminal lies has a way out.

**Reachability is deliberately under-claimed.** :func:`unreachable_under_legacy`
returns True only for combinations whose legacy encoding is *known*, and
False -- "assume it works" -- for everything else. A false "this key won't
work" is worse than no annotation at all: it sends a user chasing their
terminal for a bug that is DOXA's.
"""

from __future__ import annotations

import re

from . import config as config_mod

KITTY = "kitty"
LEGACY = "legacy"
UNKNOWN = "unknown"

PROTOCOLS = (KITTY, LEGACY, UNKNOWN)
ENV_VAR = "DOXA_KEYBOARD_PROTOCOL"

# The kitty protocol's own support query, then Primary Device Attributes.
# Order matters: DA is emitted second so its reply arrives second, which
# makes "DA came back, the u query did not" a positive observation of a
# legacy terminal rather than a timeout we chose to interpret.
QUERY = "\x1b[?u\x1b[c"

# Long enough for a round trip through tmux and an ssh hop, short enough
# that a terminal which ignores both queries costs a third of a second at
# startup, once, ever.
PROBE_TIMEOUT_SECS = 0.3

_RE_KITTY_REPLY = re.compile(r"\x1b\[\?[0-9;]*u")
_RE_DA_REPLY = re.compile(r"\x1b\[\?[0-9;]*c")

# Probe result, settled at most once per process (see module docstring).
_detected: "str | None" = None


def _is_tty() -> bool:
    """Probe seam: True only when the REAL stdin and stdout are both an
    interactive terminal. Both, because the probe writes to one and reads
    the answer from the other -- a process whose stdout is a terminal but
    whose stdin is a file would write escapes at a user and then wait for
    an answer that cannot come."""
    import sys

    try:
        return bool(
            sys.__stdin__ is not None
            and sys.__stdout__ is not None
            and sys.__stdin__.isatty()
            and sys.__stdout__.isatty()
        )
    except Exception:  # noqa: BLE001 -- a closed stream is "not a terminal"
        return False


def _ask_terminal() -> str:
    """Write :data:`QUERY`, read what comes back, classify it.

    Raw mode for the duration (the reply must not be line-buffered or
    echoed at the user), restored in a ``finally`` even if the read
    explodes. Never raises: :func:`_probe` owns the failure path."""
    import os
    import select
    import sys
    import termios
    import time
    import tty

    fd = sys.__stdin__.fileno()
    before = termios.tcgetattr(fd)
    buffer = ""
    try:
        tty.setraw(fd)
        sys.__stdout__.write(QUERY)
        sys.__stdout__.flush()
        deadline = time.monotonic() + PROBE_TIMEOUT_SECS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _w, _x = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            buffer += chunk.decode("ascii", "replace")
            # The sentinel answered: everything the terminal was going to
            # say about the u query has already been said.
            if _RE_DA_REPLY.search(buffer):
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, before)
    if not _RE_DA_REPLY.search(buffer):
        # Nobody answered the question we KNOW every terminal answers, so
        # we were not the one holding this terminal's input. That says
        # nothing about the keyboard protocol, and we say nothing about it.
        return UNKNOWN
    return KITTY if _RE_KITTY_REPLY.search(buffer) else LEGACY


def _probe() -> str:
    """One measurement. Never raises -- no tty, no ``termios`` (Windows),
    a terminal that hangs up mid-read: all of them are "unknown", which is
    a fact about our knowledge, not an error."""
    try:
        if not _is_tty():
            return UNKNOWN
        return _ask_terminal()
    except Exception:  # noqa: BLE001 -- a failed probe is ignorance, not a crash
        return UNKNOWN


def detect_protocol() -> str:
    """The keyboard protocol in force: the ``DOXA_KEYBOARD_PROTOCOL``
    override when set to a known value, else the (once-)probed answer."""
    forced = config_mod.raw(ENV_VAR).strip().lower()
    if forced in PROTOCOLS:
        return forced
    global _detected
    if _detected is None:
        _detected = _probe()
    return _detected


# -- reachability ------------------------------------------------------
#
# Legacy encoding, stated once so the predicate below can be read against
# it. Ctrl+<key> exists only where the pair has a C0 code: the 26 letters,
# and @ [ \ ] ^ _ ? and space. Ctrl with any other printable character --
# a comma, a digit, a hyphen -- produces no byte at all, so the key is not
# merely ambiguous, it is unreachable.

_C0_CTRL_PUNCTUATION = frozenset({
    "at", "left_square_bracket", "backslash", "right_square_bracket",
    "circumflex_accent", "underscore", "question_mark",
})

# Printable characters Textual names rather than spells. Anything here is
# a character key, so Ctrl+it is subject to the C0 rule above; anything
# NOT here is assumed to be a named non-character key (arrows, F-keys,
# home/end/insert/delete/pageup/pagedown), which xterm has encoded with
# modifiers since long before the kitty protocol existed.
_NAMED_PRINTABLE = frozenset({
    "comma", "full_stop", "slash", "minus", "plus", "equals_sign",
    "semicolon", "colon", "apostrophe", "quotation_mark", "grave_accent",
    "tilde", "exclamation_mark", "number_sign", "dollar_sign",
    "percent_sign", "ampersand", "asterisk", "left_parenthesis",
    "right_parenthesis", "left_curly_bracket", "right_curly_bracket",
    "less_than_sign", "greater_than_sign", "vertical_line",
})

# Ctrl+<letter> pairs whose C0 code is a key in its own right, so Textual
# reports that OTHER key and a binding on this one never fires. Verified
# against ``textual/_ansi_sequences.py``: \x08 -> backspace, \t -> tab,
# \r -> enter, \x1b -> escape. (\n is the exception -- it maps to ctrl+j,
# so ctrl+j really is reachable and is deliberately absent here.)
_SHADOWED_BY_C0 = frozenset({"h", "i", "m", "left_square_bracket"})

# Keys whose legacy byte carries no modifier information, so any modified
# form of them is indistinguishable from the bare press. Space is in here
# rather than among the C0 punctuation above because Ctrl+Space, which
# does produce a byte (NUL), produces the byte Textual reports as
# ``ctrl+@`` -- so a binding spelled ``ctrl+space`` still never fires.
_UNMODIFIABLE = frozenset({"enter", "tab", "escape", "backspace", "space"})

# Modifiers the legacy encoding has no representation for whatsoever.
_IMPOSSIBLE_MODIFIERS = frozenset({"super", "hyper", "meta"})

# Alt used to be listed as fine here, on the reasoning that a terminal
# sends it as an ESC prefix and has done since long before the kitty
# protocol. The premise is true and the conclusion was wrong, because the
# question is not what the TERMINAL sends -- it is what TEXTUAL decodes.
# Measured against textual 5.3.0's own parser (v0.95.0):
#
#     XTermParser().feed("\x1bs")  -> Key('escape'), Key('s')
#     XTermParser().feed("\x1bd")  -> Key('escape'), Key('d')
#     XTermParser().feed("\x1b[115;3u") -> Key('alt+s')     # kitty
#     XTermParser().feed("\x1b[1;3D")   -> Key('alt+left')  # legacy, fine
#
# The string "alt" appears exactly once in ``textual/_xterm_parser.py``
# (line 338), inside the CSI-u modifier table -- there is no ESC-prefix
# to-Alt path in the parser at all. ``textual/_ansi_sequences.py`` maps a
# handful of two-byte ESC pairs by hand (``\x1bf`` -> ctrl+right,
# ``\x1bb`` -> ctrl+left, ``\x1b\x7f`` -> ctrl+w) and no letter to Alt.
#
# So under the legacy encoding a binding on ``alt+<character>`` can never
# fire: the app is handed a bare Escape and then the naked letter, which
# a focused prompt happily types. Alt+<NAMED key> is a different physical
# encoding -- CSI 1;3<final>, the same shape as Ctrl+arrow -- and does
# decode, which is why alt+arrow survives this and alt+letter does not.
_ALT_ONLY_UNDER_KITTY = "alt"


def unreachable_under_legacy(key: str) -> bool:
    """Can a legacy-encoding terminal deliver `key` as itself?

    `key` is spelled the way Textual spells it -- ``"ctrl+comma"``, the
    same string ``DoxaApp.BINDINGS`` and ``SlashCommand.binding`` carry.

    True means *known* unreachable. False means "no reason to think
    otherwise", which covers everything this function has not been taught,
    and that asymmetry is the point: modified cursor and function keys go
    out as ``CSI 1;5D``-style sequences that every terminal since xterm
    has sent and plain letters are fine, and a wrong claim about any of
    them would be worse than the silence it replaced.

    Alt+<character> is the one entry this function got WRONG rather than
    merely omitted, from v0.91.0 until v0.95.0 -- see the
    :data:`_ALT_ONLY_UNDER_KITTY` note for the measurement that corrected
    it. Alt+<named key> (arrows, F-keys) is still reachable and still
    says so."""
    parts = key.split("+")
    # "ctrl+@" is spelled with the character (Keys.ControlAt), unlike every
    # other punctuation key, which uses its Unicode name. Split on "+"
    # leaves a trailing empty part for "ctrl++"; neither is a case worth
    # a special branch beyond not crashing on it.
    base = parts[-1].lower() if parts else ""
    modifiers = {part.lower() for part in parts[:-1]}
    if not modifiers:
        return False
    if modifiers & _IMPOSSIBLE_MODIFIERS:
        return True
    if base in _UNMODIFIABLE:
        # Shift+Tab is the one modified form the legacy encoding does
        # carry -- CSI Z, back-tab, older than the problem this module is
        # about.
        if base == "tab" and modifiers == {"shift"}:
            return False
        return bool(modifiers & {"ctrl", "shift", _ALT_ONLY_UNDER_KITTY})
    if _ALT_ONLY_UNDER_KITTY in modifiers:
        # See the _ALT_ONLY_UNDER_KITTY note above. A CHARACTER key under
        # Alt goes out as an ESC prefix, which textual 5.3.0 does not
        # decode as Alt at all; a NAMED key goes out as CSI 1;3<final>,
        # which it does. Checked before the ctrl branch so ctrl+alt+<char>
        # -- ESC then the C0 byte, decoded no better -- lands here too.
        if len(base) == 1 or base in _NAMED_PRINTABLE:
            return True
        if base in _C0_CTRL_PUNCTUATION or base == "@":
            return True
    if "ctrl" in modifiers:
        if base in _SHADOWED_BY_C0:
            return True
        if "shift" in modifiers and len(base) == 1 and base.isalpha():
            # Ctrl+Shift+A and Ctrl+A are the same byte.
            return True
        if len(base) == 1 and base.isalpha():
            return False
        if base in _C0_CTRL_PUNCTUATION or base == "@":
            return False
        if len(base) == 1 or base in _NAMED_PRINTABLE:
            # A printable character with no C0 code: no byte exists. The
            # DIGITS live here (v0.97.0's Ctrl+1..Ctrl+9, jump to a pane
            # group by position) and they are the same bucket as Ctrl+,
            # and Ctrl+Tab, which have shipped since v0.39.0 and v0.42.0:
            # deliverable under the kitty protocol, silently dead under the
            # legacy encoding, and ANNOTATED as such wherever DOXA lists a
            # key. That annotation is the whole reason this module exists
            # -- the alternative is a documented key that does nothing and
            # never says why. `/pane <n>` is the door that always works.
            return True
    return False


def is_unreachable(key: str) -> bool:
    """Is `key` unreachable in the terminal we are ACTUALLY in?

    False unless the protocol was measured AND measured as legacy. An
    unmeasured terminal gets no annotation anywhere -- see the module
    docstring on why silence beats a confident wrong answer."""
    return detect_protocol() == LEGACY and unreachable_under_legacy(key)


def notice_enabled() -> bool:
    """``DOXA_KEY_NOTICE`` / the config file's ``key_notice`` row, default
    ON: does an affected terminal get the one-line startup notice
    (:func:`doxa.ui.labels.unreachable_notice`)? Read per call, like
    :func:`detect_protocol` and every other env-driven knob, so the
    settings modal's toggle takes effect on the next launch without a
    rebuild.

    This is the ONLY gate the caller applies -- whether there is
    anything to say at all is :func:`doxa.ui.labels.unreachable_notice`'s
    own call, off the same measured protocol this module already caches,
    so a headless run (`_is_tty` False, protocol UNKNOWN) produces no
    notice regardless of this setting rather than needing its own
    tty check."""
    raw = config_mod.raw("DOXA_KEY_NOTICE").strip()
    if not raw:
        return True
    return raw.lower() not in ("0", "false", "no", "off")


def describe() -> str:
    """One line for a bug report: which protocol, and what follows from
    it. The value of ``/about``'s keyboard row."""
    protocol = detect_protocol()
    if protocol == KITTY:
        return "kitty protocol (all bound keys reachable)"
    if protocol == LEGACY:
        return "legacy encoding (some bound keys cannot be sent -- see /help)"
    return "not measured (no terminal to ask)"
