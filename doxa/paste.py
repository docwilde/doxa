# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.paste -- the clipboard/paste helpers shared by the prompt (item N)
and, later, excerpt insertion into a running session (item J: pasting an
excerpt or copying one out goes through the same collapse/format rules so
the two features don't drift into two different definitions of "big").

Pure functions plus one best-effort subprocess probe; no Textual imports
here on purpose -- :class:`doxa.app.PromptInput` is the one place that
turns these into widget state, which is what lets the collapse threshold,
the placeholder text, and CRLF handling be tested without a running app.
"""

from __future__ import annotations

import shutil
import subprocess

# A paste that would insert more than this many lines collapses to a
# placeholder instead of filling the prompt with the raw text -- mirrors
# the convention Claude Code's own CLI uses for its "[Pasted text #N]"
# collapse. Chosen small (most one- or two-line pastes, like a stack trace
# frame or a URL, should just appear inline) rather than tuned to any
# particular terminal's paste-latency characteristics.
COLLAPSE_LINES = 4

# Bracketed paste can hand us tens of thousands of lines (the 5000-line
# case item N asks to measure); collapsing on LINE COUNT alone would still
# leave a multi-megabyte single "line" (no embedded newlines, e.g. a
# minified JSON blob) sitting in the document. Byte size is the second,
# independent trigger.
COLLAPSE_BYTES = 4096


def normalize_newlines(text: str) -> str:
    """CRLF and lone CR both become LF. Textual's ``TextArea`` document
    model is LF-only -- pasting a CRLF-terminated file without this first
    would show a stray ``^M`` (or, depending on terminal, an extra blank
    visual row) after every line."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def line_count(text: str) -> int:
    """Number of visual lines ``text`` occupies -- 0 for the empty string,
    otherwise one more than its newline count."""
    return text.count("\n") + 1 if text else 0


def should_collapse(text: str) -> bool:
    """Whether a paste this size should collapse to a placeholder rather
    than land in the prompt as-is."""
    return line_count(text) > COLLAPSE_LINES or len(text.encode("utf-8", "surrogatepass")) > COLLAPSE_BYTES


def placeholder_for(text: str) -> str:
    """``⧉ pasted N lines (X KB)`` -- the collapsed stand-in inserted at
    the cursor. The KB figure is the UTF-8 byte size, not character count,
    since that is what actually gets sent to the model."""
    n = line_count(text)
    kb = len(text.encode("utf-8", "surrogatepass")) / 1024
    return f"⧉ pasted {n} line{'s' if n != 1 else ''} ({kb:.1f} KB)"


def detect_clipboard_image_mime(timeout: float = 0.3) -> str | None:
    """Best-effort: does the system clipboard currently offer image data?

    Terminals only ever forward TEXT through bracketed paste (there is no
    escape sequence for binary clipboard content) -- so this is not called
    from the paste path itself. It exists for the one honest thing DOXA
    can do about an image on the clipboard: notice it and say so. Tries
    ``wl-paste`` (Wayland) then ``xclip`` (X11), whichever is on PATH,
    querying the OFFERED MIME TYPES rather than transferring the bytes.
    Returns the first ``image/*`` type offered, or ``None`` -- no tool
    present, a text-only clipboard, or any failure at all (a clipboard
    probe must never raise into a key-handling path)."""
    probes: list[list[str]] = []
    if shutil.which("wl-paste"):
        probes.append(["wl-paste", "--list-types"])
    if shutil.which("xclip"):
        probes.append(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"])
    for cmd in probes:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("image/"):
                return line
    return None
